# 15. Planning

**Maps to:** Planner · Executor · Guardrails · State/Context · **Distilled from:** Claude Code 2.1.88 — `src/utils/planModeV2.ts`, `src/tools/EnterPlanModeTool/`, `src/tools/ExitPlanModeTool/`, `src/tools/TodoWriteTool/`, `src/tools/Task{Create,Update,List,Get}Tool/`, plan-mode attachments in `src/utils/messages.ts`; Articraft `agent/harness.py` (implicit in-context planning)

## Why this module exists

Most agents have no planner. The model "plans in context" and the harness
hopes. That is genuinely fine for tasks with one obvious approach — and it
fails in a specific, expensive way when the task is ambiguous: the agent
resolves the ambiguity silently, in the direction it happens to prefer, and
then builds the wrong thing efficiently. By the time anyone notices, there is a
diff to argue about instead of a decision to make.

The second failure is different and shows up on long tasks: the agent does not
lose the *ability* to finish, it loses *track*. Ten steps in, with the first
three steps' context compacted away, "what was I doing" has no reliable answer.

Planning as a module answers both: a phase before execution where ambiguity is
resolved and an approach is agreed, and a durable, externally visible record of
where execution has got to.

## How Claude Code implements it

### Planning is a permission mode, not an instruction

This is the load-bearing decision. Plan mode is a value of
`PermissionMode` (`types/permissions.ts:16-22`), so mutating tools are
*unavailable* while planning — enforced by the permission ladder (reference
13), not requested in the prompt. The model that is merely *told* not to edit
will eventually edit; the model that *cannot* edit explores instead.

Everything else in this module is downstream of that. Read-only-ness is what
makes it safe to spend twenty turns exploring, which is what makes the plan
worth reading.

### The tool description says when to enter, in examples

`EnterPlanMode` is described as proactive — "use this tool proactively when
you're about to start a non-trivial implementation task" — with four numbered
trigger conditions, each carrying a concrete example of the *ambiguity* that
justifies it (`tools/EnterPlanModeTool/prompt.ts:23-40`):

> **New Feature Implementation** — "Add a logout button" — where should it go?
> What should happen on click?
> **Multiple Valid Approaches** — "Add caching to the API" — could use Redis,
> in-memory, file-based…

The examples do the work. "Use plan mode for complex tasks" is unactionable;
"here is a one-line request that hides three decisions" is a pattern the model
can match against.

### Phases, each with its own failure mode

The documented workflow the model is actually shown is explore → understand
existing patterns → design → present for approval → clarify via
AskUserQuestion → exit (`tools/EnterPlanModeTool/prompt.ts:4-13`). Naming
phases separately is worth doing because each fails differently: exploration
fails by reading too little, design by producing something unreviewable,
approval by being skipped, execution by losing track.

**Two variants exist, and the one this reference is easiest to read as
describing is the rare one.** An "interview phase" adds an explicit
clarify-first step, gated by `isPlanModeInterviewPhaseEnabled`
(`utils/planModeV2.ts:50-60`). The source states the split plainly: the
separate five-phase workflow "is 99% of plan traffic; interview-phase (ants) is
untouched as a reference population" (`utils/planModeV2.ts:66-71`). Treat an
explicit interview step as a *deliberate variant* worth adopting, not as what
production does by default — and note that `templates/agentkit/planner.py`
starts in `INTERVIEW`, which is a choice, not a port.

The variant also decides where the instructions live: the "What Happens"
section is *omitted* from the tool description when an attachment supplies it
(`tools/EnterPlanModeTool/prompt.ts:16-21`). Two sources of the same
instruction is a maintenance bug waiting to happen; this picks one at runtime.

### Exploration fans out, and only exploration

Plan mode spawns **3 read-only Explore agents** by default
(`getPlanModeV2ExploreAgentCount`, `utils/planModeV2.ts:31-43`), and 1-3 plan
agents depending on subscription tier (`getPlanModeV2AgentCount:5-29`).

Exploration is the phase that parallelises cleanly — independent readers, no
shared writes, and the answers compose. Design does not (three plans is not a
plan), and execution does not without worktree isolation (reference 09). A
planner that fans out the wrong phase produces either contradictions or
conflicts.

### The plan is a file before it is a decision

`ExitPlanMode` **does not take the plan as a parameter**. The model writes the
plan to the plan file first; the tool "simply signals that you're done planning
and ready for the user to review" (`tools/ExitPlanModeTool/prompt.ts:1-10`).

Three things follow. The plan survives the turn that produced it. Approval
refers to an artifact both parties can point at. And the plan can be revised
without re-running the conversation that produced it.

**Approval is not universally un-bypassable, and the exception is instructive.**
`requiresUserInteraction()` returns true for a normal session but **false for a
teammate agent** (`tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts:185-194`),
whose comment reads: "If isPlanModeRequired(): team lead approves via mailbox —
Otherwise: exits locally without approval (voluntary plan mode)." So the
authority moves rather than disappearing — to a team lead, or to nobody when
plan mode was voluntary. Two lessons, and the second is the load-bearing one:
in a multi-agent system, "who approves" is a routing question, not a boolean;
and a *voluntary* plan mode that exits without approval is a planning aid, not
a control. Do not describe it as a control.

A related honesty point about the same file: `isReadOnly()` returns **false**
(`ExitPlanModeV2Tool.ts:182-183`, commented "Now writes to disk") because the
exit tool itself writes. Plan mode is read-only for the *work*, not for every
tool in it.

### The mode reminds the model that it is in the mode

Plan-mode attachments are re-injected every **5 turns**, with a full
restatement every **5th** attachment and a one-liner otherwise
(`utils/attachments.ts:259-262`, `utils/messages.ts:3385-3399`). A mode the model has
forgotten it is in is worse than no mode: it produces an agent that keeps
trying to edit and keeps being refused. Alternating full and sparse is the
compromise — a full reminder every turn is ignored within three turns.

### Execution is tracked by a separate, externally visible list

The plan says what will happen; the todo list says where we are. Its rules are
narrow on purpose (`tools/TodoWriteTool/prompt.ts:3-25`): use it for 3+ step
work, mark a task `in_progress` **before** starting and `completed`
immediately after, and keep **exactly one** task in progress. The one-in-progress
invariant is what makes the list a statement about *now* rather than a wish
list — and it is what lets "what was I doing" survive compaction.

Equally important is the negative half, which occupies as much of the
description as the positive half: do **not** use it for a single straightforward
task, for fewer than three trivial steps, or for conversational requests.
An always-available bookkeeping tool that fires on trivia teaches the model to
perform process instead of work.

The list renders to a panel rather than the transcript —
`renderToolResultMessage` is deliberately omitted (`Tool.ts:561-566`) — so
tracking costs no context. Its durable multi-agent successor splits the same
state into `TaskCreate/Get/Update/List`, so tasks can be assigned to teammates
and each verb is separately permissionable (`constants/tools.ts:77-88`).

### Planning composes with delegation, not instead of it

`Workflow` exists for the case where the *orchestration* should be
deterministic — loops, fan-out, barriers in a script rather than in the model's
judgement (reference 09). The division: a plan is for deciding **what** to do
when that is unclear; a workflow is for executing a **known** shape reliably.
Reaching for a workflow to resolve ambiguity, or a plan to execute a known
pipeline, gets you the worst of both.

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Planning is a permission **mode** | A model told not to edit will edit; a model that cannot edit explores instead | Needs a permission layer able to express it; the mode must be visibly exitable or it feels like a trap |
| Named phases with distinct failure modes | Interview, explore, design and execute fail differently and need different guards | More state; a rigid machine annoys on tasks that need only one phase |
| Trigger conditions given as ambiguity examples | "Use for complex tasks" is unactionable; "one line hiding three decisions" is matchable | Examples date, and bias toward the domains they came from |
| Fan out **exploration** only | Readers compose; three plans do not, and parallel writes conflict | Exploration cost multiplies; needs distinct angles or the agents duplicate each other |
| The plan is written to a file before approval | Approval refers to a durable artifact, survives the turn, and can be revised independently | An extra write; the file must be found again on resume |
| Exit takes no plan parameter | Prevents a plan that exists only inside one tool call | Two steps where authors expect one |
| Approval requires a *named* authority, not merely a flag | An auto-approved plan is not a plan; but in a multi-agent system the approver may be a team lead rather than the local user | Headless and teammate paths need an explicit routing decision; "voluntary" plan mode approves nothing and must not be sold as a control |
| Mode reminders every 5 turns, full every 5th | Forgetting the mode produces repeated refused edits; reminding every turn is ignored | Two more constants; still context cost |
| Todo list separate from the plan | The plan is intent, the list is position; conflating them means editing intent to record progress | Two artifacts to keep consistent |
| Exactly one task in progress | Makes the list a claim about now, and survives compaction | Genuinely parallel work has to be modelled as separate agents |
| The todo description spends half its length on when NOT to use it | An always-available tool fires on trivia and trains process-performance | The model may under-use it on borderline tasks |
| Todo renders to a panel, not the transcript | Tracking should not cost context | Progress is invisible in a replayed transcript unless recorded separately |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| Explore agents in plan mode | 3 (`utils/planModeV2.ts:31-43`) | Enough for distinct angles; beyond it they duplicate |
| Plan agents | 1, or 3 on higher tiers (`utils/planModeV2.ts:5-29`) | Multiple plans are only worth it when you will judge between them |
| Plan length vs rejection | p50 4,906 chars, p90 11,617; reject rate 20% under 2K rising to 50% at 20K+ (`utils/planModeV2.ts:76-78`) | The most decision-relevant number here: longer plans are rejected more, monotonically |
| Env override bound | 1-10 agents (`planModeV2.ts:9, 37`) | Refuses a fan-out nobody can read |
| Plan-mode reminder cadence | every 5 turns (`utils/attachments.ts:259-262`) | Below it the reminder is ignored; above it the mode is forgotten |
| Full vs sparse reminder | full every 5th (`utils/attachments.ts:261`) | Restatement is expensive; a one-liner keeps the mode present |
| Todo threshold | 3+ distinct steps (`tools/TodoWriteTool/prompt.ts:9`) | Below this, tracking costs more than it returns |
| Tasks in progress | at most 1 — the source says "**ideally** … only one" (`tools/TodoWriteTool/prompt.ts:14`) | The list is a claim about now. The template enforces at-most-one; the source states it as strong guidance, not an invariant |
| Todo reminder | after 10 turns without a write, at most every 10 (`utils/attachments.ts:254-257`) | Catches an abandoned list without nagging |
| Plan-mode tools | read/search/ask/todo/exit only | The phase where a stray write does most damage — nothing has been agreed yet |

## Reusable pattern

`templates/agentkit/planner.py` implements the phase machine, the guards, the
reminder cadence and the todo invariant. The spine:

```python
class Phase(str, Enum):
    INTERVIEW = "interview"; EXPLORE = "explore"; DESIGN = "design"
    AWAITING_APPROVAL = "awaiting_approval"; EXECUTE = "execute"

PLAN_MODE_TOOLS = {"Read", "Glob", "Grep", "AskUserQuestion",
                   "TodoWrite", "ExitPlanMode"}          # allowlist

def tool_allowed(self, name):        # enforced, not requested
    return (not self.read_only) or name in PLAN_MODE_TOOLS

def advance(self, to):
    # The guards are where the machine earns its keep.
    if to is Phase.DESIGN and self.unresolved_questions:
        return False, ("unresolved questions: … ask them before designing — a "
                       "plan built on a guess is cheaper to fix now than after "
                       "approval")
    if to is Phase.AWAITING_APPROVAL and not self.plan.exists():
        return False, (f"no plan file at {self.plan.path}. Write the plan "
                       "first; exiting only signals it is ready to review")
    if to is Phase.EXECUTE and not self.plan.approved:
        return False, "the plan has not been approved"
    ...

def explore_prompts(question, n=3):
    # Distinct ANGLES, not n copies. Redundant explorers find the same things.
    angles = ["where is this implemented today — files, entry points, call paths",
              "what conventions must this match — similar features, tests, naming",
              "what could break — callers, tests, config, implicit contracts",
              "prior art — past attempts, reverts, and why the shape is what it is"]
    return [f"{question}\n\nYour angle: {a}" for a in angles[:n]]
```

Two wiring rules:

- Refuse a mutating tool during planning with a message that names the exit:
  "…write your plan to `<path>` and call ExitPlanMode". A bare "not allowed"
  makes the model retry.
- Tick the reminder from the loop, not from a tool: the mode has to persist
  across turns in which no relevant tool was called.

## Pitfalls

- **Planning as a prompt instruction.** It will be ignored under pressure —
  which is exactly when it mattered. Make it a mode the permission layer
  enforces.
- **A plan that exists only in a message.** It cannot be revised, referenced
  after compaction, or pointed at during a disagreement. Write it to a file.
- **Approving a plan the user has not seen.** If approval can be auto-allowed,
  the whole phase is theatre.
- **Fanning out design or execution.** Three plans is not a plan; parallel
  writes conflict. Fan out exploration only.
- **Redundant explorers.** N copies of the same question return N copies of the
  same answer. Give each a distinct angle.
- **No mode reminder.** The model forgets it is planning and spends turns
  getting edits refused.
- **A reminder every turn.** Ignored by turn three, and it costs context
  forever.
- **Conflating plan and progress.** Editing the plan to record progress
  destroys the record of what was agreed.
- **More than one task in progress.** The list stops being a claim about now
  and becomes a wish list.
- **A todo tool with no "when not to use".** It fires on trivia and the model
  learns to perform process instead of doing work.
- **Using a plan where a workflow belongs.** If the shape is already known,
  script it; model judgement is for the parts that are actually uncertain.

## Checklist

- [ ] Planning is a mode the permission layer enforces, not a prompt request
- [ ] The mode's tool set is an allowlist
- [ ] Entry conditions are described with concrete ambiguity examples
- [ ] Phases are named, and transitions are guarded (no designing with open questions)
- [ ] Only exploration fans out, and each explorer gets a distinct angle
- [ ] The plan is written to a durable file before approval is requested
- [ ] The exit tool takes no plan content, and approval routes to a named
      authority (local user, team lead, or explicitly nobody — but say which)
- [ ] Approval binds to the plan's *content*, so a post-approval rewrite is
      caught rather than executed
- [ ] A refused tool names the exit path, not just the refusal
- [ ] Mode reminders are rate-limited and alternate full/sparse
- [ ] Progress is tracked separately from the plan, with exactly one item active
- [ ] The tracking tool documents when NOT to use it
- [ ] Progress tracking does not consume transcript context
- [ ] The plan file is recoverable on resume
