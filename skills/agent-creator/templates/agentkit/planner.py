"""Planning: turning a vague request into an approved, trackable plan.

Distilled from Claude Code 2.1.88 ``src/utils/planModeV2.ts``,
``src/tools/EnterPlanModeTool/``, ``src/tools/ExitPlanModeTool/``,
``src/tools/TodoWriteTool/`` and the ``plan_mode`` attachment machinery in
``src/utils/attachments.ts`` / ``src/utils/messages.ts``.

Most agents have no planner at all: the model "plans in context" and the
harness hopes. That works until the task is ambiguous, at which point the agent
confidently builds the wrong thing quickly. Claude Code's answer has three
parts, and the interesting one is the first:

1. **Planning is a MODE, not a prompt.** While planning, mutating tools are
   unavailable — enforced by the permission layer, not requested in the system
   prompt. An agent that is *told* not to edit will edit; an agent that
   *cannot* edit will explore instead. See reference 13.

2. **The plan is a durable artifact before it is approved.** Exit does not take
   the plan as a parameter — the model writes it to a file first, and exiting
   only signals readiness. Approval then refers to something that exists on
   disk and survives the turn.

3. **Execution is tracked separately, with exactly one task in progress.** The
   plan says what will happen; the todo list says where we are.

The phases are worth naming because each has a different failure mode:
*interview* fails by guessing, *explore* by reading too little, *design* by
producing something unreviewable, *execute* by losing track.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Sequence


class Phase(str, Enum):
    INTERVIEW = "interview"   # resolve ambiguity BEFORE reading a thousand files
    EXPLORE = "explore"       # read the code; still no writes
    DESIGN = "design"         # write the plan to disk
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTE = "execute"       # writes unlocked, todo list tracks progress
    DONE = "done"             # success terminal
    ABANDONED = "abandoned"   # terminal failure — and still read-only


#: Tools available while planning. An allowlist: planning is the phase where a
#: stray write does the most damage, because nobody has agreed to anything yet.
PLAN_MODE_TOOLS = frozenset({
    "Read", "Glob", "Grep", "ToolSearch", "Skill", "AskUserQuestion",
    "TodoWrite", "ExitPlanMode",
})

#: Re-inject the mode reminder every N turns; restate it fully every Nth
#: reminder. A mode the model forgets it is in is worse than no mode.
REMINDER_EVERY_TURNS = 5
FULL_REMINDER_EVERY = 5

#: How many read-only explorers to fan out during EXPLORE. Exploration is the
#: one phase that parallelises cleanly: independent readers, no shared writes.
DEFAULT_EXPLORE_AGENTS = 3

TODO_STATUSES = ("pending", "in_progress", "completed")


@dataclass
class Todo:
    content: str
    status: str = "pending"


@dataclass
class Plan:
    """The plan as a durable artifact, not a message."""

    path: str
    title: str = ""
    approved: bool = False
    #: Digest of the file at the moment it was approved. Approval must bind to
    #: CONTENT, not to a boolean: otherwise the agent can rewrite the plan
    #: after sign-off and execute something nobody agreed to.
    approved_digest: str | None = None

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def read(self) -> str:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def digest(self) -> str:
        return hashlib.sha256(self.read().encode("utf-8")).hexdigest()

    def unchanged_since_approval(self) -> bool:
        return self.approved and self.approved_digest == self.digest()


@dataclass
class PlanSession:
    """The phase machine. One object the loop and the permission layer share."""

    plan: Plan
    phase: Phase = Phase.INTERVIEW
    todos: list[Todo] = field(default_factory=list)
    turns_in_phase: int = 0
    reminders_sent: int = 0
    unresolved_questions: list[str] = field(default_factory=list)

    # -- capability ------------------------------------------------------

    #: Only EXECUTE unlocks writes. ABANDONED is a terminal FAILURE, so it
    #: must stay read-only — otherwise abandoning a plan is a way to obtain
    #: full write access with nothing approved.
    _WRITABLE = frozenset({Phase.EXECUTE})

    @property
    def read_only(self) -> bool:
        return self.phase not in self._WRITABLE

    def tool_allowed(self, tool_name: str) -> bool:
        return (not self.read_only) or tool_name in PLAN_MODE_TOOLS

    def rejection_for(self, tool_name: str) -> str:
        """Why a tool is unavailable, and what to do instead.

        A bare "not allowed in plan mode" makes the model retry. Naming the exit
        makes it either finish planning or explore instead.
        """
        return (
            f"{tool_name} is unavailable while planning ({self.phase.value}). "
            "Plan mode is read-only so nothing is changed before the approach is "
            "agreed. Keep exploring with Read/Glob/Grep, or write your plan to "
            f"{self.plan.path} and call ExitPlanMode to request approval."
        )

    # -- transitions ------------------------------------------------------

    def advance(self, to: Phase) -> tuple[bool, str]:
        """Attempt a transition. Returns (ok, reason).

        Transitions are guarded rather than free: the guards are where the
        phase machine earns its keep. Skipping straight to DESIGN with open
        questions is exactly how an agent produces a confident wrong plan.
        """
        legal = {
            Phase.INTERVIEW: {Phase.EXPLORE, Phase.DESIGN, Phase.ABANDONED},
            Phase.EXPLORE: {Phase.DESIGN, Phase.INTERVIEW, Phase.ABANDONED},
            Phase.DESIGN: {Phase.AWAITING_APPROVAL, Phase.EXPLORE, Phase.ABANDONED},
            Phase.AWAITING_APPROVAL: {Phase.EXECUTE, Phase.DESIGN, Phase.ABANDONED},
            Phase.EXECUTE: {Phase.DONE, Phase.ABANDONED},
            Phase.DONE: set(),
            Phase.ABANDONED: set(),
        }
        if to not in legal[self.phase]:
            return False, f"cannot go from {self.phase.value} to {to.value}"
        if to is Phase.DONE and not self.complete:
            return False, "not every task is completed"
        if to is Phase.DESIGN and self.unresolved_questions:
            return False, (
                "there are unresolved questions: "
                + "; ".join(self.unresolved_questions)
                + ". Ask them before designing — a plan built on a guess is "
                "cheaper to fix now than after approval."
            )
        if to is Phase.AWAITING_APPROVAL and not self.plan.exists():
            return False, (
                f"no plan file at {self.plan.path}. Write the plan first; exiting "
                "plan mode only signals that it is ready to review."
            )
        if to is Phase.EXECUTE:
            if not self.plan.approved:
                return False, "the plan has not been approved"
            if not self.plan.unchanged_since_approval():
                return False, (
                    f"{self.plan.path} changed after it was approved. Approval "
                    "binds to the content that was reviewed — re-request it."
                )
        self.phase = to
        # Reset the turn counter but NOT the reminder counter: resetting both
        # makes the first reminder in every phase a full restatement, which is
        # how a rate-limited reminder becomes a per-phase lecture.
        self.turns_in_phase = 0
        return True, ""

    def approve(self) -> tuple[bool, str]:
        if self.phase is not Phase.AWAITING_APPROVAL:
            return False, f"nothing is awaiting approval (phase={self.phase.value})"
        digest = self.plan.digest()
        ok, why = self._try(Phase.EXECUTE, lambda: setattr_all(
            self.plan, approved=True, approved_digest=digest))
        return ok, why

    def _try(self, to: Phase, prepare) -> tuple[bool, str]:
        """Apply *prepare*, attempt the transition, roll back on refusal."""
        before = (self.plan.approved, self.plan.approved_digest)
        prepare()
        ok, why = self.advance(to)
        if not ok:
            self.plan.approved, self.plan.approved_digest = before
        return ok, why

    def reject(self, reason: str = "") -> tuple[bool, str]:
        """Send an awaiting plan back for revision."""
        if self.phase is not Phase.AWAITING_APPROVAL:
            return False, f"nothing is awaiting approval (phase={self.phase.value})"
        self.plan.approved = False
        self.plan.approved_digest = None
        ok, why = self.advance(Phase.DESIGN)
        if ok and reason:
            self.unresolved_questions.append(reason)
        return ok, why

    # -- questions --------------------------------------------------------

    def ask(self, question: str) -> None:
        """Record an open question. Blocks the move into DESIGN."""
        if question not in self.unresolved_questions:
            self.unresolved_questions.append(question)

    def answered(self, question: str) -> None:
        if question in self.unresolved_questions:
            self.unresolved_questions.remove(question)

    # -- reminders --------------------------------------------------------

    def tick(self) -> str | None:
        """Call once per turn. Returns a reminder to inject, or None.

        Rate-limited and alternating between a full restatement and a one-liner:
        a reminder on every turn is ignored within three turns, and a reminder
        that never repeats is forgotten after a long tool batch.
        """
        self.turns_in_phase += 1
        if self.phase in (Phase.DONE, Phase.ABANDONED):
            return None
        if self.turns_in_phase % REMINDER_EVERY_TURNS:
            return None
        if not self.read_only:
            # EXECUTE is the phase the docstring says fails by losing track,
            # so it gets a reminder too — about progress, not about the mode.
            self.reminders_sent += 1
            return (
                f"<plan-progress>{self.progress()}. Keep the task list current: "
                "mark the active task completed before starting the next.</plan-progress>"
            )
        self.reminders_sent += 1
        if self.reminders_sent % FULL_REMINDER_EVERY == 1:
            return (
                f"<plan-mode phase=\"{self.phase.value}\">\n"
                "You are still in plan mode. Nothing you do can change files "
                "yet. Finish understanding the problem, then write your plan to "
                f"{self.plan.path} and call ExitPlanMode for approval. Use "
                "AskUserQuestion for decisions that are genuinely the user's.\n"
                "</plan-mode>"
            )
        return f"<plan-mode phase=\"{self.phase.value}\">Still planning — no writes yet.</plan-mode>"

    # -- execution tracking ----------------------------------------------

    def set_todos(self, todos: Sequence[Todo]) -> tuple[bool, str]:
        # Validate statuses FIRST: counting in_progress over a list containing
        # "in-progress" miscounts, and then the invariant check passes on data
        # that was never valid.
        for t in todos:
            if t.status not in TODO_STATUSES:
                return False, (
                    f"invalid status {t.status!r}; expected one of "
                    f"{', '.join(TODO_STATUSES)}"
                )
        running = [t for t in todos if t.status == "in_progress"]
        if len(running) > 1:
            return False, (
                f"{len(running)} tasks are in_progress. At most one may be — "
                "the list is a statement about now, not a wish list."
            )
        self.todos = list(todos)
        return True, ""

    def progress(self) -> str:
        if not self.todos:
            return "no tasks tracked"
        done = sum(1 for t in self.todos if t.status == "completed")
        active = next((t.content for t in self.todos if t.status == "in_progress"), None)
        return f"{done}/{len(self.todos)} complete" + (f" — now: {active}" if active else "")

    @property
    def complete(self) -> bool:
        return bool(self.todos) and all(t.status == "completed" for t in self.todos)


def setattr_all(obj: Any, **kw: Any) -> None:
    for k, v in kw.items():
        setattr(obj, k, v)


#: Distinct exploration angles. Redundant explorers find the same things, so
#: the value of parallel exploration is that the angles are blind to each other
#: — which caps how many are worth running.
_ANGLES = (
    "Locate the code that currently implements or is closest to this, and "
    "report the exact files, entry points and call paths.",
    "Find the existing conventions this change must match: similar features, "
    "their structure, naming, tests and error handling.",
    "Find what could break: callers, tests, configuration, migrations and "
    "any implicit contracts that depend on current behaviour.",
    "Find prior art in history: past attempts, reverts, and comments "
    "explaining why the current shape is what it is.",
)


def explore_prompts(question: str, n: int = DEFAULT_EXPLORE_AGENTS) -> list[str]:
    """Fan out exploration along DIFFERENT axes, not n copies of one question."""
    if n < 1:
        raise ValueError("n must be at least 1")
    if n > len(_ANGLES):
        raise ValueError(
            f"only {len(_ANGLES)} distinct exploration angles are defined; "
            f"asking for {n} would duplicate them, and redundant explorers "
            "return redundant answers"
        )
    return [f"{question}\n\nYour angle: {a}" for a in _ANGLES[:n]]
