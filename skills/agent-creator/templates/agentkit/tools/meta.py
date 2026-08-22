"""ToolSearch and Skill — the two tools that manage the agent's own surface.

Ported from Claude Code 2.1.88 ``src/tools/ToolSearchTool`` and
``src/tools/SkillTool``. Both implement the same ladder at different levels:
announce a name, load the payload only when asked.

``ToolSearch`` must never itself be deferred — a loader you cannot reach is a
dead end. ``Skill`` must never be deferred either, for a subtler reason: the
model has to know skills exist before it can decide one applies.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contract import Context, Permission, ToolResult, Validation, build_tool
from registry import TOOL_SEARCH_NAME, Pool, search
from skills_loader import Skill, render_body


def make_tool_search(get_pool: Callable[[], Pool]) -> Any:
    """Build the schema-fetch tool. *get_pool* returns the live pool."""

    def _call(input_: dict[str, Any], ctx: Context) -> ToolResult:
        pool = get_pool()
        matches, already = search(pool, input_["query"], int(input_.get("max_results") or 5))
        if not matches:
            if already:
                # Not a failure: the model can call these right now.
                return ToolResult.success(
                    f"Already loaded and callable: {', '.join(already)}. "
                    "No fetch was needed — call them directly.",
                    data={"already_loaded": already},
                )
            available = pool.withheld_names
            return ToolResult.failure(
                f"No withheld tool matched {input_['query']!r}. "
                + (
                    f"Withheld tools are: {', '.join(available)}. "
                    'Use "select:Name" to fetch one exactly.'
                    if available
                    else "No tools are currently withheld — every schema is already loaded."
                ),
                code="no_match",
            )
        # THE load-bearing line: fetching a schema is what makes the tool
        # callable. Without it the dispatcher keeps refusing and the model
        # loops until the turn budget runs out.
        pool.mark_loaded(t.name for t in matches)
        # Return schemas in exactly the encoding the model already reads tool
        # definitions in, so a fetched tool is indistinguishable from a
        # preloaded one.
        body = "\n".join(
            "<function>" + json.dumps(
                {
                    "description": t.description,
                    "name": t.name,
                    "parameters": t.input_schema,
                },
                ensure_ascii=False,
            ) + "</function>"
            for t in matches
        )
        note = (
            f"\n\nAlready loaded (no fetch needed): {', '.join(already)}" if already else ""
        )
        return ToolResult.success(
            f"<functions>\n{body}\n</functions>{note}",
            data={"loaded": [t.name for t in matches], "already_loaded": already},
        )

    return build_tool(
        name=TOOL_SEARCH_NAME,
        description=(
            "Fetch full schema definitions for deferred tools so they can be called.\n\n"
            "Deferred tools appear by name in system messages. Until fetched, only "
            "the name is known — there is no parameter schema, so the tool cannot be "
            "invoked. Once a schema appears in this tool's result, it is callable "
            "exactly like any tool defined up front.\n\n"
            "Query forms:\n"
            '- "select:Read,Edit" — fetch these exact tools by name\n'
            '- "notebook jupyter" — keyword search, best matches first\n'
            '- "+slack send" — require "slack" in the name, rank by the rest'
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "select:… , keywords, or +required"},
                "max_results": {"type": "integer", "description": "Default 5"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        call=_call,
        is_read_only=lambda _i: True,
        is_concurrency_safe=lambda _i: True,
        check_permissions=lambda i, c: Permission(Permission.ALLOW),
        always_load=True,          # the loader can never be behind the loader
        max_result_chars=100_000,
        activity=lambda i: f"Loading tool schemas for {i.get('query','')}",
    )


def make_skill_tool(
    get_skills: Callable[[], Sequence[Skill]],
    running: set[str] | None = None,
) -> Any:
    """Build the Skill tool over a live skill list.

    *running* tracks skills expanded in the current turn, so a skill whose body
    says "use the X skill" cannot re-enter itself forever.
    """
    running = running if running is not None else set()

    def _find(name: str, skills: Sequence[Skill]) -> Skill | None:
        name = name.lstrip("/").strip()
        return next((s for s in skills if s.name == name), None)

    def _validate(input_: dict[str, Any], ctx: Context) -> Validation:
        raw = str(input_.get("skill", "")).strip()
        if not raw:
            return Validation.invalid("skill must be a non-empty name", code=1)
        skills = get_skills()
        skill = _find(raw, skills)
        if skill is None:
            names = ", ".join(sorted(s.name for s in skills)) or "(none loaded)"
            return Validation.invalid(
                f"Unknown skill: {raw.lstrip('/')}. Available skills: {names}", code=2
            )
        if not skill.model_invocable:
            return Validation.invalid(
                f"Skill {skill.name} has disable-model-invocation set and can only "
                "be run by the user.", code=4,
            )
        if skill.name in running:
            return Validation.invalid(
                f"Skill {skill.name} is already loaded in this turn — follow the "
                "instructions you already have instead of invoking it again.", code=7,
            )
        return Validation.valid()

    def _check(input_: dict[str, Any], ctx: Context) -> Permission:
        skill = _find(str(input_["skill"]), get_skills())
        if skill is None:
            return Permission(Permission.PASSTHROUGH)
        # Allowlist, not blocklist: a skill declaring only known-safe keys runs
        # without a prompt; anything else asks. A capability added to the skill
        # format next month therefore defaults to asking.
        if not skill.unsafe_keys:
            return Permission(Permission.ALLOW, reason={"type": "safe_properties"})
        return Permission(
            Permission.ASK,
            message=(
                f"Run skill {skill.name}? It declares: {', '.join(skill.unsafe_keys)}"
            ),
            reason={"type": "rule_ask", "detail": "unreviewed skill properties"},
            suggestions=(
                {"tool": "Skill", "content": skill.name, "scope": "exact"},
            ),
        )

    def _call(input_: dict[str, Any], ctx: Context) -> ToolResult:
        skill = _find(str(input_["skill"]), get_skills())
        assert skill is not None      # validate_input already proved this
        running.add(skill.name)
        return ToolResult.success(
            render_body(skill, str(input_.get("args") or "")),
            data={"skill": skill.name, "base_dir": skill.base_dir},
        )

    return build_tool(
        name="Skill",
        description=(
            "Load and follow a named skill's instructions in this conversation.\n\n"
            "- Available skills are listed in system messages with a one-line "
            "description; this tool loads the full body.\n"
            "- When a skill matches the request, invoke it BEFORE answering about "
            "the task — the body may change the whole approach.\n"
            "- Never mention a skill without actually calling this tool.\n"
            "- If the body is already present in this turn, follow it; do not "
            "invoke again."
        ),
        search_hint="invoke a named skill by name",
        input_schema={
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "The skill name"},
                "args": {"type": "string", "description": "Optional arguments"},
            },
            "required": ["skill"],
            "additionalProperties": False,
        },
        call=_call,
        validate_input=_validate,
        check_permissions=_check,
        is_read_only=lambda _i: True,
        always_load=True,     # the model must know skills exist on turn 1
        # A skill body is content the model must actually read; relocating it to
        # disk would defeat the purpose of loading it.
        max_result_chars=100_000,
        rule_key=lambda i: str(i.get("skill", "")).lstrip("/"),
        activity=lambda i: f"Running skill {i.get('skill','')}",
    )
