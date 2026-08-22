"""Delegate — hand a scoped task to a subagent.

Distilled from Claude Code 2.1.88 `src/tools/AgentTool/`, wired to the
declarative `AgentDefinition` in `orchestration.py`.

Delegation is how an agent stays inside its context window on work that does
not fit in it: a subagent reads forty files and returns four sentences. The
danger is that it is also how an agent quietly acquires capabilities nobody
granted it, so almost all of the code below is about what the child does NOT
get:

* its tool allowlist **replaces** the parent's rather than extending it, so a
  parent's approvals cannot leak downward;
* a fixed set is denied to every subagent regardless of definition, each with
  its reason recorded;
* the result is typed, so the caller can tell "here is the answer" from
  "here is a handle" without parsing prose.
"""
from __future__ import annotations

import os
import sys
import uuid
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contract import Context, Permission, ToolResult, Validation, build_tool
from orchestration import AgentDefinition, Delegation, resolve_subagent_tools

#: A subagent that never returns is worse than one that fails: the parent
#: waits, and the turn budget drains with nothing to show.
DEFAULT_MAX_TURNS = 20


def make_delegate_tool(
    definitions: Mapping[str, AgentDefinition],
    run_agent: Callable[[AgentDefinition, str, Sequence[str], dict[str, Any]], Delegation],
    *,
    parent_pool: Callable[[], Sequence[str]] = lambda: (),
    background: Callable[[AgentDefinition, str, Sequence[str]], str] | None = None,
) -> Any:
    """Build the tool over a registry of agent definitions.

    *run_agent* receives ``(definition, prompt, allowed_tools, options)`` and
    returns a `Delegation`. It owns the actual loop; this tool owns who is
    allowed to run and with what.

    *background*, if given, launches asynchronously and returns an output
    path — worth having, because a forty-file read should not block the parent
    on work it will not look at for several turns.
    """

    def _agent_listing() -> str:
        # The listing goes in the DESCRIPTION only because this registry is
        # static. If yours changes during a session — plugins loading, servers
        # connecting — move it to a message: a mutating tool description
        # invalidates the whole tool-schema cache on every change.
        return "\n".join(
            f"- {d.agent_type}: {d.when_to_use}"
            + (f" (tools: {', '.join(d.tools)})" if d.tools else " (inherits the tool pool)")
            for d in definitions.values()
        )

    def _validate(input_: dict[str, Any], ctx: Context) -> Validation:
        kind = str(input_.get("agent_type", "")).strip()
        if kind not in definitions:
            known = ", ".join(sorted(definitions)) or "(none registered)"
            return Validation.invalid(
                f"Unknown agent type {kind!r}. Available: {known}", code=2
            )
        if not str(input_.get("prompt", "")).strip():
            return Validation.invalid(
                "prompt is required — a subagent shares no context with you, so "
                "it needs the whole task, not a reference to what you were doing.",
                code=1,
            )
        if input_.get("run_in_background") and background is None:
            return Validation.invalid(
                "Background delegation is not configured in this deployment.", code=5
            )
        return Validation.valid()

    def _call(input_: dict[str, Any], ctx: Context) -> ToolResult:
        definition = definitions[input_["agent_type"]]
        prompt = input_["prompt"]
        allowed, refused = resolve_subagent_tools(definition, parent_pool())
        agent_id = f"{definition.agent_type.lower()}-{uuid.uuid4().hex[:8]}"

        if input_.get("run_in_background") and background is not None:
            path = background(definition, prompt, allowed)
            out = Delegation.launched(definition.agent_type, agent_id, path)
            return ToolResult.success(
                f"Launched {definition.agent_type} in the background ({agent_id}). "
                f"Read its output at {path} when you need it — you do not have to wait.",
                data=out.to_dict(),
            )

        options = {
            "max_turns": definition.max_turns or DEFAULT_MAX_TURNS,
            "model": definition.model,
            "isolation": definition.isolation,
            "omit_project_instructions": definition.omit_project_instructions,
            "refused_tools": refused,
        }
        try:
            result = run_agent(definition, prompt, allowed, options)
        except Exception as exc:  # noqa: BLE001
            # A subagent that dies is a tool failure, not a parent crash.
            return ToolResult.failure(
                f"{definition.agent_type} failed: {type(exc).__name__}: {exc}",
                code="subagent_error",
            )
        if result.status == "failed":
            return ToolResult.failure(
                f"{definition.agent_type} failed: {result.error}", code="subagent_failed"
            )
        return ToolResult.success(result.result, data=result.to_dict())

    return build_tool(
        name="Delegate",
        description=(
            "Hand a self-contained task to a subagent and get its report back.\n\n"
            "Use it when the work needs to read far more than it will produce — "
            "surveying a codebase, checking many files against one rule — so the "
            "reading happens in the subagent's context instead of yours.\n\n"
            "The subagent shares NO context with you: it sees only the prompt you "
            "write, and you see only what it returns. Write the task in full, and "
            "say what shape of answer you want back.\n\n"
            "Available agents:\n" + _agent_listing()
        ),
        search_hint="delegate work to a subagent",
        input_schema={
            "type": "object",
            "properties": {
                "agent_type": {"type": "string", "description": "Which agent to run"},
                "prompt": {"type": "string",
                           "description": "The complete, self-contained task"},
                "run_in_background": {"type": "boolean",
                                      "description": "Return a handle instead of waiting"},
            },
            "required": ["agent_type", "prompt"],
            "additionalProperties": False,
        },
        call=_call,
        validate_input=_validate,
        # Delegation itself writes nothing; the child's tools are separately
        # permissioned inside its own run.
        is_read_only=lambda _i: True,
        check_permissions=lambda i, c: Permission(Permission.ALLOW),
        max_result_chars=100_000,
        rule_key=lambda i: str(i.get("agent_type", "")),
        activity=lambda i: f"Delegating to {i.get('agent_type', 'a subagent')}",
    )
