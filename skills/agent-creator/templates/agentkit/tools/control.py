"""Todo and AskUser — tools that steer the agent rather than change the world.

Ported from Claude Code 2.1.88 (``src/tools/TodoWriteTool``,
``src/tools/AskUserQuestionTool``).

Both exist for the same reason: an agent working on something long either
externalises its plan and its uncertainty, or it drifts and guesses. The
interesting design content is the *negative* space —

* TodoWrite's description spends as much room on **when not to use it** as on
  when to. An always-available bookkeeping tool that fires on trivia teaches
  the model to perform process instead of work.
* AskUserQuestion is structured (2-4 labelled options, recommended first)
  because a free-text question is expensive to answer and ambiguous to parse.
  Making the question cheap is what makes the agent ask at the right moment
  instead of stalling or guessing.
"""
from __future__ import annotations

from typing import Any, Callable

from contract import Context, Permission, ToolResult, Validation, build_tool

STATUSES = ("pending", "in_progress", "completed")


# --------------------------------------------------------------------------
# Todo
# --------------------------------------------------------------------------

def make_todo_tool(store: dict[str, Any]) -> Any:
    """Build a TodoWrite bound to *store* (a dict with a ``"todos"`` key)."""

    def _validate(input_: dict[str, Any], ctx: Context) -> Validation:
        todos = input_.get("todos")
        if not isinstance(todos, list):
            return Validation.invalid("todos must be a list")
        for i, t in enumerate(todos):
            if not isinstance(t, dict):
                return Validation.invalid(f"todos[{i}] must be an object")
            if not str(t.get("content", "")).strip():
                return Validation.invalid(f"todos[{i}].content must be a non-empty string")
            if t.get("status") not in STATUSES:
                return Validation.invalid(
                    f"todos[{i}].status must be one of {', '.join(STATUSES)} "
                    f"(got {t.get('status')!r})"
                )
        # Exactly-one-in-progress is a real constraint, not a style preference:
        # it is what makes the list a statement about *now* rather than a wish.
        running = [t for t in todos if t["status"] == "in_progress"]
        if len(running) > 1:
            return Validation.invalid(
                f"{len(running)} todos are in_progress. Exactly one task may be "
                "in progress at a time — finish or re-queue the others."
            )
        return Validation.valid()

    def _call(input_: dict[str, Any], ctx: Context) -> ToolResult:
        store["todos"] = input_["todos"]
        done = sum(1 for t in store["todos"] if t["status"] == "completed")
        active = next(
            (t["content"] for t in store["todos"] if t["status"] == "in_progress"), None
        )
        summary = f"{done}/{len(store['todos'])} complete"
        if active:
            summary += f" — now: {active}"
        return ToolResult.success(summary, data={"todos": store["todos"]})

    return build_tool(
        name="TodoWrite",
        description=(
            "Maintain a structured task list for the current session.\n\n"
            "Use it when: the work has 3+ distinct steps, the user gave several "
            "tasks at once, or you are about to start a task (mark it in_progress "
            "BEFORE starting, and completed immediately after).\n\n"
            "Do NOT use it when: there is one straightforward task, the work takes "
            "fewer than three trivial steps, or the request is conversational. "
            "Tracking trivia is noise, and the user reads this list.\n\n"
            "Exactly one task may be in_progress at a time."
        ),
        search_hint="manage the session task checklist",
        input_schema={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The full list, not a delta",
                }
            },
            "required": ["todos"],
            "additionalProperties": False,
        },
        call=_call,
        validate_input=_validate,
        # Bookkeeping touches no external state, so it never needs consent —
        # but it is not read-only either: it replaces the list.
        check_permissions=lambda i, c: Permission(Permission.ALLOW),
        is_concurrency_safe=lambda _i: False,
        should_defer=True,
        activity=lambda i: "Updating the task list",
    )


# --------------------------------------------------------------------------
# AskUser
# --------------------------------------------------------------------------

def make_ask_user_tool(prompt_user: Callable[[dict[str, Any]], dict[str, str]]) -> Any:
    """Build an AskUserQuestion bound to a *prompt_user* callback.

    The callback receives the validated question payload and returns
    ``{question_header: chosen_label}``. Keeping it injected means the tool
    works identically in a TUI, a web UI, and a test.
    """

    def _validate(input_: dict[str, Any], ctx: Context) -> Validation:
        questions = input_.get("questions")
        if not isinstance(questions, list) or not (1 <= len(questions) <= 4):
            return Validation.invalid("questions must be a list of 1-4 items")
        for i, q in enumerate(questions):
            if not str(q.get("question", "")).strip():
                return Validation.invalid(f"questions[{i}].question is required")
            if not str(q.get("header", "")).strip():
                return Validation.invalid(
                    f"questions[{i}].header is required (a <=12 char chip label)"
                )
            opts = q.get("options")
            if not isinstance(opts, list) or not (2 <= len(opts) <= 4):
                return Validation.invalid(
                    f"questions[{i}].options must have 2-4 entries. If there is only "
                    "one sensible answer, do not ask — choose it and say so."
                )
            for j, o in enumerate(opts):
                if not str(o.get("label", "")).strip():
                    return Validation.invalid(f"questions[{i}].options[{j}].label is required")
                if str(o.get("label")).strip().lower() == "other":
                    return Validation.invalid(
                        "Do not add an 'Other' option — the UI always provides one."
                    )
        return Validation.valid()

    def _call(input_: dict[str, Any], ctx: Context) -> ToolResult:
        answers = prompt_user(input_)
        if not answers:
            return ToolResult.failure(
                "The user dismissed the question without answering. Proceed with "
                "the most reasonable default and say which one you chose.",
                code="dismissed",
            )
        body = "\n".join(f"{k}: {v}" for k, v in answers.items())
        return ToolResult.success(body, data={"answers": answers})

    return build_tool(
        name="AskUserQuestion",
        description=(
            "Ask the user multiple-choice questions to resolve a decision that is "
            "genuinely theirs.\n\n"
            "- 1-4 questions, each with 2-4 options. The UI always adds 'Other'.\n"
            "- If you have a recommendation, make it the first option and append "
            '"(Recommended)" to its label.\n'
            "- Reserve this for choices you cannot resolve from the request, the "
            "code, or a sensible default. For anything with a conventional answer, "
            "pick it, say so, and continue."
        ),
        search_hint="ask the user a multiple-choice question",
        input_schema={
            "type": "object",
            "properties": {
                "questions": {"type": "array", "description": "1-4 questions"}
            },
            "required": ["questions"],
            "additionalProperties": False,
        },
        call=_call,
        validate_input=_validate,
        is_read_only=lambda _i: True,
        # A question IS the interaction: it can never be auto-approved away,
        # even in a mode that skips every other prompt.
        requires_user_interaction=lambda: True,
        check_permissions=lambda i, c: Permission(Permission.ALLOW),
        should_defer=True,
        activity=lambda i: "Asking the user",
    )
