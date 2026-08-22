"""The tool-call gauntlet: everything between a tool_use block and the world.

Distilled from Claude Code 2.1.88
``src/services/tools/toolExecution.ts:599-1300``.

Seven ordered stages. Every one of them can reject, and **every rejection
becomes a tool_result the model can read and correct from** — nothing here
raises into the loop, because a raised exception leaves a tool_use block with
no matching tool_result and the next API call fails on a malformed
conversation.

    1. schema      — does the input parse at all?
    2. validate    — is it valid for this tool, cheaply checkable?
    3. sanitise    — strip fields only the harness may set
    4. backfill    — derive observer-only fields onto a COPY
    5. hooks       — PreToolUse: observe, rewrite, decide, or halt
    6. permission  — the consent ladder
    7. call        — the only stage that touches the world
   (+ PostToolUse hooks, result cap, message assembly)

The staging is not bureaucracy: the model is told *which* gate stopped it, so
"your argument was malformed" and "you are not allowed to do that" produce
different corrections.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from contract import Context, Permission, Tool, ToolResult, effective_result_cap
from hooks import HookExecutor, HookSpec, run_hooks
from permissions import PermissionContext, resolve
from registry import Pool
from result_store import apply_result_cap

#: Fields the model must never be able to set, even if a schema drifts.
#: Defence in depth behind the schema, not instead of it.
RESERVED_INPUT_FIELDS = frozenset({"_internal", "_approved", "_simulated"})


@dataclass
class Outcome:
    """What one tool call produced."""

    tool_use_id: str
    tool_name: str
    result: ToolResult
    #: Which stage decided. Useful in traces; "call" means it actually ran.
    stage: str = "call"
    duration_ms: int = 0
    permission: Permission | None = None
    #: Extra user-role messages the harness should append (hook context, etc.)
    extra_messages: list[str] = field(default_factory=list)
    halt: bool = False
    halt_reason: str | None = None

    def to_block(self, results_dir: str, cap: float) -> dict[str, Any]:
        """Render an API-legal tool_result block.

        Nothing beyond type/tool_use_id/content/is_error goes in: Anthropic
        rejects unknown keys inside a content block. Anything the harness
        needs about this call lives on the Outcome, not on the wire.
        """
        block = self.result.to_message(self.tool_use_id)
        if isinstance(block.get("content"), str):
            # Errors are capped too — a tool that fails with a 5 MB stderr
            # dump would otherwise bypass the per-result budget entirely.
            block["content"] = apply_result_cap(
                block["content"], cap, results_dir, self.tool_use_id
            )
        return block


AskFn = Callable[[Tool, dict[str, Any], Permission], bool]


def _reject(tid: str, name: str, stage: str, message: str, code: str = "") -> Outcome:
    return Outcome(
        tool_use_id=tid,
        tool_name=name,
        stage=stage,
        result=ToolResult.failure(message, code=code or stage),
    )


def validate_schema(tool: Tool, input_: dict[str, Any]) -> str | None:
    """Minimal structural check against the declared JSON schema.

    Intentionally shallow — swap in jsonschema/pydantic for production. What
    matters for the pattern is that the failure is *reported to the model with
    the field names*, not raised.
    """
    schema = tool.input_schema or {}
    if schema.get("type") == "object" and not isinstance(input_, dict):
        return f"expected an object, got {type(input_).__name__}"
    props: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])
    missing = [k for k in required if k not in input_]
    if missing:
        return f"missing required parameter(s): {', '.join(missing)}"
    if schema.get("additionalProperties") is False:
        extra = [k for k in input_ if k not in props]
        if extra:
            return (
                f"unexpected parameter(s): {', '.join(sorted(extra))}. "
                f"Allowed: {', '.join(sorted(props))}"
            )
    wrong = []
    kinds = {"string": str, "number": (int, float), "integer": int,
             "boolean": bool, "array": list, "object": dict}
    for key, spec in props.items():
        if key in input_ and (py := kinds.get(spec.get("type"))) is not None:
            if not isinstance(input_[key], py) or (
                spec.get("type") in ("number", "integer") and isinstance(input_[key], bool)
            ):
                wrong.append(f"{key} (expected {spec['type']})")
    if wrong:
        return f"invalid value(s) for: {', '.join(wrong)}"
    return None


def execute(
    pool: Pool,
    tool_use_id: str,
    tool_name: str,
    raw_input: dict[str, Any],
    context: Context,
    perm_ctx: PermissionContext,
    *,
    hook_specs: Sequence[HookSpec] = (),
    hook_executor: HookExecutor | None = None,
    ask: AskFn | None = None,
    results_dir: str = ".",
) -> Outcome:
    """Run one tool call through every gate. Never raises."""
    started = time.monotonic()

    def _guard(fn, default, label):
        """Call a tool- or hook-supplied callable without letting it escape."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            return default

    errors: list[str] = []
    tool = pool.get(tool_name)
    if tool is None:
        known = ", ".join(sorted(t.name for t in pool.tools)[:25])
        return _reject(
            tool_use_id, tool_name, "unknown_tool",
            f"No such tool: {tool_name}. Available tools: {known}",
        )

    # A withheld tool called without its schema is a specific, recoverable
    # mistake — say so, and name the fix. Once ToolSearch has fetched it,
    # `is_withheld` goes false and the call proceeds.
    if pool.is_withheld(tool.name):
        return _reject(
            tool_use_id, tool.name, "schema_not_loaded",
            f"{tool.name} is withheld: its schema was not sent, so this call "
            f"cannot be validated. Call ToolSearch with "
            f'"select:{tool.name}" first, then retry.',
        )

    if not _guard(tool.is_enabled, True, "is_enabled"):
        return _reject(tool_use_id, tool.name, "disabled",
                       f"{tool.name} is not enabled in this session.")

    # 1. Schema.
    if (err := validate_schema(tool, raw_input)) is not None:
        return _reject(tool_use_id, tool.name, "schema", f"InputValidationError: {err}")

    # 2. Tool-specific validation.
    try:
        v = tool.validate_input(raw_input, context)
    except Exception as exc:  # noqa: BLE001
        return _reject(tool_use_id, tool.name, "validate", f"Validation crashed: {exc}")
    if not v.ok:
        return _reject(tool_use_id, tool.name, "validate", v.message, code=f"e{v.code}")

    # 3. Sanitise: strip harness-only fields the model may have guessed at.
    call_input = {k: val for k, val in raw_input.items() if k not in RESERVED_INPUT_FIELDS}

    # 4. Backfill onto a COPY. Observers (hooks, permission UI, transcript) see
    #    derived fields; `call` receives the model's original values so the
    #    prompt cache and any result string that echoes the input stay stable.
    observable = dict(call_input)
    if tool.backfill_observable_input is not None:
        try:
            tool.backfill_observable_input(observable)
        except Exception:  # noqa: BLE001 - observability must never break a call
            observable = dict(call_input)

    extra: list[str] = []

    # 5. PreToolUse hooks.
    from hooks import Aggregate
    pre = _guard(
        lambda: run_hooks(
            hook_specs, "PreToolUse",
            {"tool_name": tool.name, "tool_input": observable, "cwd": context.cwd},
            hook_executor, tool_name=tool.name,
        ),
        Aggregate(), "PreToolUse hooks",
    )
    extra.extend(pre.additional_contexts)
    if pre.prevent_continuation:
        out = _reject(tool_use_id, tool.name, "hook",
                      pre.stop_reason or "Stopped by hook")
        out.halt, out.halt_reason = True, pre.stop_reason
        out.extra_messages = extra
        return out
    if pre.blocked:
        out = _reject(tool_use_id, tool.name, "hook",
                      "; ".join(pre.blocking_errors) or "Blocked by hook")
        out.extra_messages = extra
        return out
    if pre.updated_input is not None:
        call_input = pre.updated_input      # a hook that rewrites owns the shape
        observable = dict(call_input)

    # 6. Permission.
    rule_key = (
        _guard(lambda: tool.rule_key(observable), None, "rule_key")
        if tool.rule_key else None
    )
    if pre.permission == "allow":
        perm = Permission(Permission.ALLOW, reason={"type": "hook"})
    elif pre.permission == "deny":
        perm = Permission(Permission.DENY, message=pre.permission_reason or "Denied by hook",
                          reason={"type": "hook"})
    else:
        # A permission layer that raises must fail CLOSED, never open.
        perm = _guard(
            lambda: resolve(tool, observable, perm_ctx, context, rule_key=rule_key),
            Permission(Permission.DENY, message="permission check failed",
                       reason={"type": "error"}),
            "permission",
        )

    if perm.behavior == Permission.ASK:
        granted = _guard(lambda: ask(tool, observable, perm), False, "ask") if ask else False
        perm = Permission(
            Permission.ALLOW if granted else Permission.DENY,
            message=perm.message,
            reason={"type": "user", "granted": granted},
            updated_input=perm.updated_input,
        )
    if perm.behavior != Permission.ALLOW:
        out = _reject(tool_use_id, tool.name, "permission",
                      perm.message or f"Permission denied for {tool.name}")
        out.permission, out.extra_messages = perm, extra
        run_hooks(hook_specs, "PermissionDenied",
                  {"tool_name": tool.name, "tool_input": observable},
                  hook_executor, tool_name=tool.name)
        return out

    # Only an explicit replacement reaches call(). `observable` carries
    # backfilled fields that must never get there, and an identity check is
    # too weak — a permission layer that returns `dict(observable)` would slip
    # through. Require the value to differ from the observable copy.
    if perm.updated_input is not None and perm.updated_input != observable:
        call_input = perm.updated_input

    if context.aborted():
        return _reject(tool_use_id, tool.name, "aborted", "Interrupted by user")

    # 7. The call. A crashing tool becomes an error result, never an exception:
    #    the model can retry or route around it, and the conversation stays
    #    well-formed.
    try:
        result = tool.call(call_input, context)
    except Exception as exc:  # noqa: BLE001
        result = ToolResult.failure(f"{type(exc).__name__}: {exc}", code="tool_crashed")

    post = _guard(
        lambda: run_hooks(
            hook_specs, "PostToolUse" if result.ok else "PostToolUseFailure",
            {"tool_name": tool.name, "tool_input": observable,
             "tool_result": result.content if result.ok else result.error},
            hook_executor, tool_name=tool.name,
        ),
        Aggregate(), "PostToolUse hooks",
    )
    extra.extend(post.additional_contexts)
    if post.blocked and result.ok:
        # A PostToolUse block does not undo the side effect — it tells the
        # model the result is unacceptable so the next turn can repair it.
        result = ToolResult.failure(
            "; ".join(post.blocking_errors), code="post_hook_blocked"
        )

    if errors:
        extra.append(
            "Harness note (the tool still ran): " + "; ".join(errors)
        )
    out = Outcome(
        tool_use_id=tool_use_id, tool_name=tool.name, result=result, stage="call",
        duration_ms=int((time.monotonic() - started) * 1000),
        permission=perm, extra_messages=extra,
    )
    out.halt = post.prevent_continuation
    out.halt_reason = post.stop_reason
    return out


def execute_batch(
    pool: Pool,
    calls: Sequence[tuple[str, str, dict[str, Any]]],
    context: Context,
    perm_ctx: PermissionContext,
    max_workers: int = 8,
    **kw: Any,
) -> list[Outcome]:
    """Run a batch, parallelising only when EVERY call in it is safe.

    Concurrency is opt-in per call, not per tool and not per provider: one
    mutating call serialises the whole batch, because the ordering between an
    edit and a read of the same file is semantic, not incidental. Results are
    always returned in the order the model requested them, so a parallel batch
    and a serial batch are indistinguishable to the model.
    """
    resolved = [(tid, pool.get(name), inp) for tid, name, inp in calls]
    parallel_ok = len(calls) > 1 and all(
        t is not None and t.is_concurrency_safe(inp) and t.is_read_only(inp)
        for _, t, inp in resolved
    )
    run = lambda c: execute(pool, c[0], c[1], c[2], context, perm_ctx, **kw)

    if not parallel_ok:
        outcomes: list[Outcome] = []
        for call in calls:
            outcomes.append(run(call))
            # A hook that halted the turn stops the rest of the batch: the
            # remaining calls were authored against a state that no longer holds.
            if outcomes[-1].halt:
                break
        return outcomes

    with ThreadPoolExecutor(max_workers=min(max_workers, len(calls))) as pool_:
        outcomes = list(pool_.map(run, calls))
    # A halt in a parallel batch cannot un-run the others, but it must still
    # stop the turn — otherwise "parallel" and "serial" differ in behaviour,
    # which the caller has no way to predict.
    for i, o in enumerate(outcomes):
        if o.halt:
            return outcomes[: i + 1]
    return outcomes
