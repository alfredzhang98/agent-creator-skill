"""Generic verified-artifact agent turn loop.

Distills ``articraft.agent.harness`` (turn loop, no-action escalation,
finish gate, verify freshness) and ``articraft.agent.events`` (typed stop
reasons) into a domain-neutral, stdlib-only skeleton.

Core invariant: the run succeeds only when the latest artifact revision
passed a *fresh* verification.  Wire in the sibling templates: tools.py
(registry + dispatch), verifier.py, provider_adapter.py, cost_meter.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

Message = dict[str, Any]  # {"role": ..., "content": ...} plus provider extras


class StopReason(str, Enum):
    GOAL_COMPLETE = "goal_complete"
    MAX_TURNS = "max_turns"
    COST_LIMIT = "cost_limit"
    ERROR = "error"


@dataclass
class RunResult:
    success: bool
    reason: StopReason
    message: str = ""
    artifact: Any = None
    turns: int = 0        # completed loop turns (model produced text or calls)
    llm_calls: int = 0    # every provider round-trip, incl. no-action turns


class Provider(Protocol):
    """See provider_adapter.py."""

    def complete(self, messages: list[Message],
                 tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Return {"text": str, "tool_calls": list, "usage": dict}."""
        ...


class ToolRegistry(Protocol):
    """See tools.py."""

    def schemas(self) -> list[dict[str, Any]]: ...
    def dispatch(self, call: dict[str, Any]) -> tuple[Message, bool]: ...
    def is_mutating(self, name: str) -> bool: ...


class VerifierPort(Protocol):
    """See verifier.py."""

    verify_tool_name: str

    def latest_is_fresh(self) -> bool: ...
    def mark_mutated(self) -> None: ...
    def run_cached(self) -> str: ...   # rendered signal bundle for the LLM


class CostMeterPort(Protocol):
    """See cost_meter.py."""

    def over_cap(self) -> bool: ...
    def add_turn(self, usage: dict[str, int]) -> float: ...
    def summary_line(self) -> str: ...


# No-action escalation: streak 1 -> gentle nudge, streak 2 -> hard demand;
# abort at streak 3, but only after the hard demand was actually delivered.
GENTLE_NUDGE_UNTIL = 1
ABORT_NO_ACTION_AT = 3


def _harness_msg(tag: str, text: str) -> Message:
    """Synthetic user message, XML-tagged so the model can attribute it."""
    return {"role": "user", "content": f"<{tag}>\n{text}\n</{tag}>"}


class AgentLoop:
    """One provider round-trip per iteration; typed exits; verify-gated finish."""

    def __init__(self, provider: Provider, tools: ToolRegistry,
                 verifier: VerifierPort, cost: CostMeterPort,
                 *, max_turns: int = 40, max_llm_calls: int | None = None) -> None:
        self.provider = provider
        self.tools = tools
        self.verifier = verifier
        self.cost = cost
        self.max_turns = max_turns
        # llm_calls also counts no-action turns, so bound it separately.
        self.max_llm_calls = max_llm_calls or max_turns * 2

    def run(self, conversation: list[Message]) -> RunResult:
        turns = llm_calls = no_action_streak = 0
        hard_demand_sent = False
        schemas = self.tools.schemas()

        while turns < self.max_turns and llm_calls < self.max_llm_calls:
            # --- cost gate: abort BEFORE spending money on the next call ---
            if self.cost.over_cap():
                return self._exit(False, StopReason.COST_LIMIT, turns, llm_calls)

            response = self.provider.complete(conversation, schemas)
            llm_calls += 1
            text: str = response.get("text") or ""
            calls: list[dict[str, Any]] = response.get("tool_calls") or []
            if response.get("usage"):
                self.cost.add_turn(response["usage"])
            if text.strip() or calls:   # never append an empty assistant msg
                conversation.append({"role": "assistant", "content": text,
                                     "tool_calls": calls})
            # Gate again: the response we just paid for may have crossed the cap.
            if self.cost.over_cap():
                return self._exit(False, StopReason.COST_LIMIT, turns, llm_calls)

            # --- no-action turn: nudge -> demand -> abort ------------------
            if not calls and not text.strip():
                no_action_streak += 1
                if no_action_streak >= ABORT_NO_ACTION_AT and hard_demand_sent:
                    return self._exit(False, StopReason.ERROR, turns, llm_calls,
                                      note="model produced no text and no tool calls")
                if no_action_streak <= GENTLE_NUDGE_UNTIL:
                    conversation.append(_harness_msg(
                        "reminder", "Your last reply was empty. Continue the task: "
                        "call a tool or state your conclusion."))
                else:
                    hard_demand_sent = True
                    conversation.append(_harness_msg(
                        "action_required", "Empty reply again. You MUST either call "
                        f"a tool (e.g. {self.verifier.verify_tool_name}) or finish "
                        "with a final text answer NOW."))
                continue                          # does not consume a turn
            no_action_streak, hard_demand_sent = 0, False
            turns += 1

            # --- finish attempt: text-only passes ONLY with a fresh verify --
            if text.strip() and not calls:
                if self.verifier.latest_is_fresh():
                    return self._exit(True, StopReason.GOAL_COMPLETE, turns, llm_calls)
                conversation.append(_harness_msg(
                    "verify_required", "Latest edits are not verified. Run the "
                    f"{self.verifier.verify_tool_name} tool before finishing."))
                continue

            # --- tool dispatch: every failure goes back as a message -------
            for call in calls:
                if call.get("name") == self.verifier.verify_tool_name:
                    # Verify is orchestrator-owned so the loop sees freshness.
                    conversation.append({"role": "tool",
                                         "tool_call_id": call.get("id"),
                                         "content": self.verifier.run_cached()})
                    continue
                msg, ok = self.tools.dispatch(call)   # errors arrive as data
                if ok and self.tools.is_mutating(call.get("name", "")):
                    self.verifier.mark_mutated()      # stales the verify cache
                conversation.append(msg)

        note = "" if self.verifier.latest_is_fresh() else \
            "run ended without a fresh verification"
        return self._exit(False, StopReason.MAX_TURNS, turns, llm_calls, note)

    def _exit(self, ok: bool, reason: StopReason, turns: int, llm_calls: int,
              note: str = "") -> RunResult:
        detail = note or self.cost.summary_line()
        return RunResult(ok, reason, f"{reason.value}: {detail}",
                         artifact=None, turns=turns, llm_calls=llm_calls)
