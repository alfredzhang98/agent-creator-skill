"""Hooks: user-owned code that observes and vetoes the agent's lifecycle.

Distilled from Claude Code 2.1.88 ``src/utils/hooks.ts``,
``src/services/tools/toolHooks.ts`` and ``src/entrypoints/sdk/coreSchemas.ts``
(the 27-event vocabulary).

A hook is how somebody who is not you extends your agent without forking it.
The design problem is that hooks are *arbitrary code with the agent's
authority*, so the contract has to make three things unambiguous: when they
run, how they say "stop", and what happens when they are wrong.

Protocol (both directions are supported, and both are needed):
  * **exit code** — 0 success, 2 blocking error (stderr is fed back to the
    model), anything else non-blocking error (logged, ignored).
  * **stdout JSON** — richer: change the permission decision, rewrite the tool
    input, inject context, or halt the turn.

This module ships the protocol, matching, aggregation and blocking semantics.
It deliberately ships **no process spawner**: you supply a ``HookExecutor``
for your deployment. Hooks run whatever the settings file says, so the
decision to execute is a deployment decision, not a library default.
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

#: The lifecycle points a hook may attach to. A closed vocabulary: an unknown
#: event name is a configuration error, not a silently-never-fired hook.
EVENTS = (
    "SessionStart", "SessionEnd", "Setup", "UserPromptSubmit",
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "PermissionRequest", "PermissionDenied",
    "PreCompact", "PostCompact",
    "SubagentStart", "SubagentStop",
    "Stop", "StopFailure",
    "Notification", "TaskCreated", "TaskCompleted", "TeammateIdle",
    "Elicitation", "ElicitationResult",
    "ConfigChange", "CwdChanged", "FileChanged",
    "WorktreeCreate", "WorktreeRemove", "InstructionsLoaded",
)

#: Only PreToolUse may change a permission decision. A PostToolUse or Stop
#: hook emitting `permissionDecision` is a configuration mistake, not an
#: authorisation — honouring it would let any hook grant any tool.
PERMISSION_EVENTS = frozenset({"PreToolUse", "PermissionRequest"})

#: Per-purpose defaults. One global timeout is always wrong at one end: a
#: shutdown hook must not hold the process for ten minutes, and a test-suite
#: hook needs more than a second.
DEFAULT_TIMEOUTS = {
    "SessionEnd": 1.5,
    "Notification": 5.0,
    "PreToolUse": 60.0,
    "PostToolUse": 60.0,
    "Stop": 120.0,
}
DEFAULT_TIMEOUT = 600.0


def default_timeout_for(event: str) -> float:
    return DEFAULT_TIMEOUTS.get(event, DEFAULT_TIMEOUT)

EXIT_OK = 0
EXIT_BLOCKING = 2


@dataclass(frozen=True)
class HookSpec:
    """One configured hook."""

    event: str
    command: str
    #: Glob matched against the tool name (``"*"``/None = every tool).
    matcher: str | None = None
    #: None means "use the per-event default" — see `default_timeout_for`.
    timeout_s: float | None = None
    source: str = "user"  # policy | user | project | local | plugin


@dataclass(frozen=True)
class HookRun:
    """Raw outcome of executing one hook."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass
class HookOutcome:
    """Everything one hook asked the harness to do."""

    ok: bool = True
    blocking_error: str | None = None
    #: 'allow' | 'deny' | 'ask' | None (None = no opinion)
    permission: str | None = None
    permission_reason: str | None = None
    updated_input: dict[str, Any] | None = None
    additional_context: str | None = None
    system_message: str | None = None
    prevent_continuation: bool = False
    stop_reason: str | None = None
    source: str = ""


class HookExecutor(Protocol):
    """Runs one hook command with the event payload on stdin."""

    def run(self, spec: HookSpec, payload: dict[str, Any]) -> HookRun: ...


class RefusingHookExecutor:
    """Default executor: refuses, with an actionable message.

    Executing hooks means running commands from a settings file that may have
    arrived with a cloned repository. Whether that is acceptable — and behind
    which trust gate — is a property of your deployment, so this package makes
    you decide rather than deciding for you.
    """

    def run(self, spec: HookSpec, payload: dict[str, Any]) -> HookRun:
        raise NotImplementedError(
            "No HookExecutor configured. Hooks run arbitrary commands from "
            "configuration files, so this package ships no spawner. Implement "
            "HookExecutor.run(spec, payload) for your deployment: enforce a "
            "trust gate before the first execution, pass the payload as JSON on "
            "stdin, capture stdout/stderr separately, and enforce spec.timeout_s "
            "with a hard kill. Then pass it to run_hooks(executor=...)."
        )


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def matching_hooks(
    specs: Sequence[HookSpec], event: str, tool_name: str | None = None
) -> list[HookSpec]:
    out = []
    for s in specs:
        if s.event != event:
            continue
        if s.matcher in (None, "", "*"):
            out.append(s)
        elif tool_name and fnmatch.fnmatch(tool_name, s.matcher):
            out.append(s)
    return out


# --------------------------------------------------------------------------
# Output parsing
# --------------------------------------------------------------------------

#: Shown verbatim to a hook author whose JSON did not validate. Naming the
#: expected shape in the error is what turns a broken hook into a fixed hook.
SCHEMA_HINT = json.dumps(
    {
        "continue": "boolean (optional) - false halts the turn",
        "stopReason": "string (optional) - shown when continue is false",
        "decision": '"approve" | "block" (optional)',
        "reason": "string (optional) - required when decision is block",
        "systemMessage": "string (optional) - shown to the user, not the model",
        "hookSpecificOutput": {
            "hookEventName": '"PreToolUse" | "PostToolUse" | "UserPromptSubmit"',
            "permissionDecision": '"allow" | "deny" | "ask"  (PreToolUse only)',
            "permissionDecisionReason": "string (optional)",
            "updatedInput": "object (optional) - replaces the tool input",
            "additionalContext": "string - injected for the model to read",
        },
    },
    indent=2,
)


def parse_hook_output(run: HookRun, spec: HookSpec) -> HookOutcome:
    """Turn one raw execution into an outcome.

    Order matters: the exit code is authoritative about *blocking*, and JSON
    refines everything else. A hook that exits 2 has blocked, whatever its
    stdout said.
    """
    out = HookOutcome(source=spec.command)

    if run.timed_out:
        # Fail CLOSED. A security hook that times out must not silently
        # contribute nothing; on a permission event, no answer is a refusal.
        out.ok = False
        limit = spec.timeout_s if spec.timeout_s is not None else default_timeout_for(spec.event)
        out.system_message = f"Hook timed out after {limit}s: {spec.command}"
        if spec.event in PERMISSION_EVENTS:
            out.permission = "deny"
            out.blocking_error = f"Hook timed out and could not authorise: {spec.command}"
        return out

    text = (run.stdout or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            out.ok = False
            out.system_message = (
                f"Hook emitted invalid JSON ({exc}).\nExpected schema:\n{SCHEMA_HINT}"
            )
            data = None
        if isinstance(data, dict):
            _apply_json(out, data, spec)

    if run.exit_code == EXIT_BLOCKING:
        # The exit code is authoritative about blocking, so it OVERRIDES a
        # JSON body claiming approval. A hook that exits 2 has blocked,
        # whatever its stdout said.
        out.ok = False
        out.blocking_error = (run.stderr or run.stdout or "Blocked by hook").strip()
        out.permission = "deny"
    elif run.exit_code != EXIT_OK:
        # Non-blocking failure: surfaced to the operator, invisible to the model.
        out.ok = False
        out.system_message = (
            out.system_message
            or f"Hook failed (exit {run.exit_code}): {spec.command}\n{run.stderr.strip()}"
        )
    return out


def _apply_json(out: HookOutcome, data: dict[str, Any], spec: HookSpec) -> None:
    if data.get("continue") is False:
        out.prevent_continuation = True
        out.stop_reason = data.get("stopReason")

    decision = data.get("decision")
    if decision == "approve":
        out.permission = "allow"
    elif decision == "block":
        out.permission = "deny"
        out.blocking_error = data.get("reason") or "Blocked by hook"
        out.ok = False

    if isinstance(data.get("systemMessage"), str):
        out.system_message = data["systemMessage"]

    specific = data.get("hookSpecificOutput")
    if isinstance(specific, dict):
        declared = specific.get("hookEventName")
        if declared is not None and declared != spec.event:
            out.system_message = (
                f"Hook declared hookEventName={declared!r} but is registered on "
                f"{spec.event!r}; its hook-specific output was ignored."
            )
            return
        pd = specific.get("permissionDecision")
        if pd in ("allow", "deny", "ask"):
            # Only a permission event may decide permission.
            if spec.event in PERMISSION_EVENTS:
                out.permission = pd
                out.permission_reason = specific.get("permissionDecisionReason")
            else:
                out.system_message = (
                    f"A {spec.event} hook returned permissionDecision; only "
                    f"{'/'.join(sorted(PERMISSION_EVENTS))} hooks may decide permission."
                )
        if isinstance(specific.get("updatedInput"), dict):
            out.updated_input = specific["updatedInput"]
        if isinstance(specific.get("additionalContext"), str):
            out.additional_context = specific["additionalContext"]


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

@dataclass
class Aggregate:
    """The combined verdict of every hook on one event."""

    blocking_errors: list[str] = field(default_factory=list)
    permission: str | None = None
    permission_reason: str | None = None
    updated_input: dict[str, Any] | None = None
    additional_contexts: list[str] = field(default_factory=list)
    system_messages: list[str] = field(default_factory=list)
    prevent_continuation: bool = False
    stop_reason: str | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocking_errors) or self.permission == "deny"


#: Strictness order: the most restrictive answer from ANY hook wins. One hook
#: saying "deny" is never overridden by another saying "allow" — otherwise hook
#: order silently becomes a security parameter.
_STRICTNESS = {"allow": 0, "ask": 1, "deny": 2}


def aggregate(outcomes: Sequence[HookOutcome]) -> Aggregate:
    agg = Aggregate()
    for o in outcomes:
        if o.blocking_error:
            agg.blocking_errors.append(o.blocking_error)
        if o.permission is not None:
            if agg.permission is None or _STRICTNESS[o.permission] > _STRICTNESS[agg.permission]:
                agg.permission = o.permission
                agg.permission_reason = o.permission_reason
        if o.updated_input is not None:
            # A hook that vetoed the call does not get to rewrite its input.
            # Accepting both would run input authored by the hook that
            # refused it.
            if o.permission in (None, "allow", "ask") and not o.blocking_error:
                agg.updated_input = o.updated_input
            else:
                agg.system_messages.append(
                    f"Ignored updatedInput from a hook that denied the call: {o.source}"
                )
        if o.additional_context:
            agg.additional_contexts.append(o.additional_context)
        if o.system_message:
            agg.system_messages.append(o.system_message)
        if o.prevent_continuation:
            agg.prevent_continuation = True
            agg.stop_reason = agg.stop_reason or o.stop_reason
    return agg


def run_hooks(
    specs: Sequence[HookSpec],
    event: str,
    payload: dict[str, Any],
    executor: HookExecutor | None = None,
    tool_name: str | None = None,
    on_error: Callable[[Exception, HookSpec], None] | None = None,
) -> Aggregate:
    """Run every hook matching *event* and combine the verdicts.

    A hook that crashes the executor is recorded and skipped: a broken hook
    must not take the agent down with it, and must not silently become an
    approval either — it contributes nothing, and step 3 of the permission
    ladder still defaults to ``ask``.
    """
    if event not in EVENTS:
        raise ValueError(f"unknown hook event {event!r}; expected one of {EVENTS}")
    matched = matching_hooks(specs, event, tool_name)
    for spec in matched:
        if spec.timeout_s is None:
            object.__setattr__(spec, "timeout_s", default_timeout_for(event))
    if not matched:
        return Aggregate()
    executor = executor or RefusingHookExecutor()
    outcomes: list[HookOutcome] = []
    for spec in matched:
        try:
            outcomes.append(parse_hook_output(executor.run(spec, payload), spec))
        except Exception as exc:  # noqa: BLE001
            if on_error:
                on_error(exc, spec)
            outcomes.append(
                HookOutcome(ok=False, system_message=f"Hook error: {exc}", source=spec.command)
            )
    return aggregate(outcomes)
