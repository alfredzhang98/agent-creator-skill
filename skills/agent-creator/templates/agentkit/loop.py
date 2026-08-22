"""The agent loop as an explicit state machine with named transitions.

Distilled from Claude Code 2.1.88 ``src/query.ts`` (``queryLoop``) and
Articraft ``agent/harness.py``.

The single most useful structural idea here: **every exit and every
continuation has a name.** Claude Code's loop returns one of ten ``Terminal``
reasons and records one of seven ``Continue`` reasons on the state it carries
forward, with the stated purpose that "tests can assert recovery paths fired
without inspecting message contents" (query.ts:214-216).

That is worth copying for a reason beyond testing: a loop whose control flow is
a named state machine is a loop you can *reason* about. "Why did this run
stop?" has an answer that is a value, not an archaeology exercise across a
transcript.

The recovery ladder is the other half. When something goes wrong the loop does
not give up and does not blindly retry — it walks an ordered list of
increasingly expensive repairs, each one attempted at most once:

    context too large  -> drop cheap context -> summarise -> surface the error
    output truncated   -> raise the cap      -> ask to continue -> surface
    the model stalled  -> nudge              -> demand      -> abort
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Protocol, Sequence

from contract import Context
from hooks import HookExecutor, HookSpec, run_hooks
from permissions import PermissionContext
from pipeline import Outcome, execute_batch
from registry import Pool, announce_deferred
from result_store import apply_message_budget

Message = dict[str, Any]


class Stop(str, Enum):
    """Every way the loop can end. Closed set, exhaustively handled."""

    COMPLETED = "completed"                # the model answered with no tool calls
    MAX_TURNS = "max_turns"
    COST_LIMIT = "cost_limit"
    CONTEXT_EXHAUSTED = "context_exhausted"   # recovery ladder ran out
    NO_ACTION = "no_action"                # model stalled; escalation delivered
    HOOK_STOPPED = "hook_stopped"
    ABORTED = "aborted"
    MODEL_ERROR = "model_error"


class Continue(str, Enum):
    """Why the loop went round again. Recorded on state, asserted in tests."""

    NEXT_TURN = "next_turn"                # ordinary: tools ran, feed results back
    SHED = "shed"                          # dropped cheap context, retrying
    COMPACTED = "compacted"                # summarised, retrying
    OUTPUT_LIMIT_ESCALATE = "output_limit_escalate"
    OUTPUT_LIMIT_RESUME = "output_limit_resume"
    NO_ACTION_NUDGE = "no_action_nudge"
    HOOK_BLOCKING = "hook_blocking"
    GUIDANCE_INJECTED = "guidance_injected"


@dataclass
class Budget:
    """Hard limits, checked in the loop rather than hoped for."""

    max_turns: int = 40
    max_usd: float | None = None
    spent_usd: float = 0.0

    def exhausted(self) -> bool:
        return self.max_usd is not None and self.spent_usd >= self.max_usd


@dataclass
class State:
    """Everything carried between iterations. One assignment per continue site."""

    messages: list[Message]
    turn: int = 1
    no_action_streak: int = 0
    escalation_delivered: bool = False
    compaction_attempted: bool = False
    output_limit_escalated: bool = False
    output_limit_resumes: int = 0
    #: Set by the escalate rung and threaded into the next request.
    max_output_tokens: int | None = None
    #: Cheap context relief is tried before expensive summarisation.
    shed_attempted: bool = False
    transition: Continue | None = None


@dataclass
class Result:
    stop: Stop
    messages: list[Message]
    turns: int
    detail: str = ""
    transitions: list[Continue] = field(default_factory=list)


class Model(Protocol):
    """One provider round-trip.

    Returns ``{"text": str, "tool_calls": [(id, name, input)], "usage": {...},
    "error": str | None}``. ``error`` is a *typed* string —
    ``"context_too_long"``, ``"output_truncated"`` — not a message: the loop
    dispatches on it, so map your provider's errors onto that vocabulary.

    ``max_output_tokens`` exists because the cheapest repair for a truncated
    response is the same request with more room. Without it that rung of the
    recovery ladder burns a full generation for a guaranteed-identical result.
    """

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]: ...


MAX_OUTPUT_RESUMES = 3
#: What the escalate rung actually raises the cap to.
ESCALATED_MAX_OUTPUT_TOKENS = 64_000
NO_ACTION_ESCALATE_AT = 2      # second silent turn switches from nudge to demand
NO_ACTION_ABORT_AT = 3         # abort only after the demand was actually sent


def _user(text: str) -> Message:
    return {"role": "user", "content": text}


def _append_user(messages: list[Message], text: str) -> list[Message]:
    """Append user text, merging into a trailing user message.

    Two consecutive user messages are rejected outright by some providers and
    are cache-hostile everywhere else. The no-action path would otherwise emit
    exactly that, because a silent turn produces no assistant message to sit
    between them.
    """
    if messages and messages[-1].get("role") == "user" and isinstance(
        messages[-1].get("content"), str
    ):
        merged = dict(messages[-1])
        merged["content"] = f"{merged['content']}\n\n{text}"
        return messages[:-1] + [merged]
    return messages + [_user(text)]


def run(
    model: Model,
    pool: Pool,
    context: Context,
    perm_ctx: PermissionContext,
    initial_messages: list[Message],
    *,
    budget: Budget | None = None,
    hook_specs: Sequence[HookSpec] = (),
    hook_executor: HookExecutor | None = None,
    ask: Callable[..., bool] | None = None,
    shed: Callable[[list[Message]], list[Message] | None] | None = None,
    compact: Callable[[list[Message]], list[Message] | None] | None = None,
    cost_of: Callable[[dict[str, Any]], float] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    results_dir: str = ".",
) -> Result:
    """Run the agent until something in the closed ``Stop`` set happens."""
    budget = budget or Budget()
    messages = list(initial_messages)
    # Tell the model which tools exist but are not loaded. Without this the
    # whole deferral mechanism is invisible and it never calls the search tool.
    if (notice := announce_deferred(pool)):
        messages = [_user(notice)] + messages
    state = State(messages=messages)
    transitions: list[Continue] = []
    emit = on_event or (lambda _k, _d: None)

    #: Exits where the model never produced a usable response. Running Stop
    #: hooks on these creates the death spiral upstream documents:
    #: error -> hook blocks -> retry -> error, injecting tokens each cycle.
    FAILED_EXITS = {Stop.MODEL_ERROR, Stop.CONTEXT_EXHAUSTED, Stop.ABORTED}

    def finish(stop: Stop, detail: str = "", stop_hooks_ran: bool = False) -> Result:
        emit("stop", {"reason": stop.value, "detail": detail, "turn": state.turn})
        if stop in FAILED_EXITS:
            run_hooks(hook_specs, "StopFailure",
                      {"reason": stop.value, "detail": detail}, hook_executor)
        elif not stop_hooks_ran:
            # Only when the finish gate did not already run them.
            run_hooks(hook_specs, "Stop", {"reason": stop.value}, hook_executor)
        return Result(stop, state.messages, state.turn, detail, transitions)

    def go(reason: Continue, **updates: Any) -> None:
        nonlocal state
        transitions.append(reason)
        emit("continue", {"reason": reason.value, "turn": state.turn})
        state = replace(state, transition=reason, **updates)

    while True:
        if context.aborted():
            return finish(Stop.ABORTED)
        if state.turn > budget.max_turns:
            return finish(Stop.MAX_TURNS, f"reached max_turns={budget.max_turns}")
        # Checked BEFORE the call as well as after: a turn that starts over
        # budget must not buy one more generation.
        if budget.exhausted():
            return finish(
                Stop.COST_LIMIT, f"${budget.spent_usd:.2f} of ${budget.max_usd:.2f}"
            )

        emit("turn_start", {"turn": state.turn})
        try:
            response = model.complete(
                state.messages, pool.schemas(), state.max_output_tokens
            )
        except Exception as exc:  # noqa: BLE001 - a provider crash is an exit, not a traceback
            return finish(Stop.MODEL_ERROR, f"{type(exc).__name__}: {exc}")

        if cost_of:
            budget.spent_usd += cost_of(response.get("usage") or {})
            # Post-call check as well as pre-call: a single turn can cross the
            # cap on its own, and a run that only checks on entry reports
            # COMPLETED after blowing the budget.
            if budget.exhausted():
                return finish(
                    Stop.COST_LIMIT,
                    f"${budget.spent_usd:.2f} of ${budget.max_usd:.2f} "
                    "(crossed during this turn)",
                )

        # ---- typed error recovery ladder --------------------------------
        error = response.get("error")
        if error == "context_too_long":
            # Cheapest and most reversible first: drop re-derivable content
            # (old tool results, stale attachments) before summarising, so a
            # recoverable turn keeps its granular history.
            if shed and not state.shed_attempted:
                if (lighter := shed(state.messages)):
                    go(Continue.SHED, messages=lighter, shed_attempted=True)
                    continue
                state = replace(state, shed_attempted=True)
            if compact and not state.compaction_attempted:
                compacted = compact(state.messages)
                if compacted:
                    go(Continue.COMPACTED, messages=compacted, compaction_attempted=True)
                    continue
            # Ladder exhausted. Surfacing the real reason beats a generic
            # failure: "I ran out of room" is actionable, "error" is not.
            return finish(Stop.CONTEXT_EXHAUSTED, "context too long and compaction failed")

        if error == "output_truncated":
            if not state.output_limit_escalated:
                # Cheapest repair first: the SAME request with more room. This
                # only helps if the cap is actually raised — re-sending an
                # identical request truncates identically.
                go(Continue.OUTPUT_LIMIT_ESCALATE, output_limit_escalated=True,
                   max_output_tokens=ESCALATED_MAX_OUTPUT_TOKENS)
                continue
            if state.output_limit_resumes < MAX_OUTPUT_RESUMES:
                go(
                    Continue.OUTPUT_LIMIT_RESUME,
                    messages=state.messages
                    + [
                        {"role": "assistant", "content": response.get("text", "")},
                        _user(
                            "Output limit hit. Resume directly — no apology, no recap. "
                            "Pick up mid-thought if that is where the cut happened, and "
                            "break the remaining work into smaller pieces."
                        ),
                    ],
                    output_limit_resumes=state.output_limit_resumes + 1,
                )
                continue
            return finish(Stop.MODEL_ERROR, "output truncated repeatedly")

        if error:
            return finish(Stop.MODEL_ERROR, str(error))

        text = (response.get("text") or "").strip()
        calls = list(response.get("tool_calls") or [])

        # ---- no action: the model said and did nothing -------------------
        if not text and not calls:
            streak = state.no_action_streak + 1
            if streak >= NO_ACTION_ABORT_AT and state.escalation_delivered:
                # Abort only once the hard demand was provably delivered —
                # otherwise a transient empty response burns the run.
                return finish(Stop.NO_ACTION, f"{streak} empty responses")
            nudge = (
                "You returned nothing. Either call a tool to make progress, or "
                "reply with your final answer. Do not return an empty response."
                if streak >= NO_ACTION_ESCALATE_AT
                else "Continue — call a tool or give your answer."
            )
            go(
                Continue.NO_ACTION_NUDGE,
                messages=_append_user(state.messages, nudge),
                no_action_streak=streak,
                escalation_delivered=state.escalation_delivered
                or streak >= NO_ACTION_ESCALATE_AT,
                # A no-action turn still consumes the turn budget, so silence
                # can never loop forever under the cap.
                turn=state.turn + 1,
            )
            continue

        assistant: Message = {"role": "assistant", "content": text}
        if calls:
            assistant["tool_calls"] = [
                {"id": cid, "name": name, "input": inp} for cid, name, inp in calls
            ]

        # ---- finish attempt ----------------------------------------------
        if not calls:
            stop_agg = run_hooks(
                hook_specs, "Stop", {"text": text}, hook_executor
            )
            if stop_agg.blocking_errors:
                # A Stop hook that blocks is asking for more work, not failing.
                go(
                    Continue.HOOK_BLOCKING,
                    messages=state.messages
                    + [assistant, _user("; ".join(stop_agg.blocking_errors))],
                    turn=state.turn + 1,
                )
                continue
            state = replace(state, messages=state.messages + [assistant])
            return finish(Stop.COMPLETED, stop_hooks_ran=True)

        # ---- tools --------------------------------------------------------
        outcomes: list[Outcome] = execute_batch(
            pool,
            calls,
            context,
            perm_ctx,
            hook_specs=hook_specs,
            hook_executor=hook_executor,
            ask=ask,
            results_dir=results_dir,
        )
        emit("tools", {"turn": state.turn, "count": len(outcomes),
                       "stages": [o.stage for o in outcomes]})

        blocks = [o.to_block(results_dir, _cap(pool, o.tool_name)) for o in outcomes]
        # Per-message budget: N results each legally sized can still bury a turn.
        # Exemption is by index — the wire block carries only API-legal fields.
        exempt_idx = frozenset(
            i for i, o in enumerate(outcomes)
            if (t := pool.get(o.tool_name)) is not None
            and t.max_result_chars == float("inf")
        )
        blocks, within_budget = apply_message_budget(
            blocks, results_dir, exempt_indices=exempt_idx
        )
        if not within_budget:
            emit("budget_not_met", {"turn": state.turn, "blocks": len(blocks)})

        followups = [_user(m) for o in outcomes for m in o.extra_messages]
        next_messages = state.messages + [assistant, {"role": "user", "content": blocks}] + followups

        halted = next((o for o in outcomes if o.halt), None)
        if halted is not None:
            state = replace(state, messages=next_messages)
            return finish(Stop.HOOK_STOPPED, halted.halt_reason or "stopped by hook")

        go(
            Continue.GUIDANCE_INJECTED if followups else Continue.NEXT_TURN,
            messages=next_messages,
            turn=state.turn + 1,
            # Any productive turn clears the stall counters entirely.
            no_action_streak=0,
            escalation_delivered=False,
            compaction_attempted=False,
            output_limit_escalated=False,
            output_limit_resumes=0,
            max_output_tokens=None,
            shed_attempted=False,
        )


def _cap(pool: Pool, tool_name: str) -> float:
    from contract import effective_result_cap

    tool = pool.get(tool_name)
    return effective_result_cap(tool) if tool else 50_000.0
