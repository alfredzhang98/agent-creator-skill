---
name: agent-creator
description: >-
  Build a complete agent from one sentence, or design, review and debug any
  single part of one — loop, tools, verifier, prompts, memory, planning,
  permissions, sandboxing, cost, orchestration. Distilled from two production
  codebases.
---

# Agent Creator

## Overview

This skill packages the architecture of production LLM agents into reusable
modules. It was built by systematically reverse-engineering working agent
codebases (see `case-studies/`) and extracting, for every subsystem: the
mechanisms, the design decisions with their tradeoffs, the exact constants
used in production, a generic reusable code pattern, and the pitfalls the
design guards against.

Use it in two directions:

1. **Building a new agent** — pick the modules your agent needs, read the
   matching reference, start from the matching template.
2. **Debugging an existing agent** — find the failure mode in a reference's
   *Pitfalls* section and apply the documented guard.

## Agent anatomy

Every module maps to one slot in this reference anatomy (detailed mapping in
`references/00-agent-anatomy.md`):

```
input/task → LLM (brain) → decide: answer? tool? plan?
           → tools/APIs/code → results → update state/memory → next turn → output
```

| Anatomy slot | What it does | Reference |
|---|---|---|
| LLM / Policy | The brain: reasoning + decisions | `05-providers.md` |
| System Prompt | Identity, goals, rules, behavior | `06-prompts.md` |
| Tools | Search, code, files, compile, domain actions | `02-tools.md` |
| Planner | Decompose goals into steps, get sign-off, track progress | `15-planning.md`, `09-orchestration.md` |
| Executor | Run each step, dispatch tools | `01-agent-loop.md` |
| State / Context | Where the task is, what tools returned | `08-state-persistence.md` |
| Memory | Cross-task knowledge, retrieval | `14-memory.md`, `08-state-persistence.md` |
| Skills | Reusable specialised abilities, disclosed progressively | `11-skills-progressive-disclosure.md`, `10-action-space-sdk.md` |
| Extension points | Third-party code in your lifecycle | `12-hooks-and-extension.md` |
| Evaluator / Verifier | Is the result correct? Is the task done? | `03-evaluator-verifier.md` (gating **and** advisory tiers) |
| Guardrails | Permissions, safety, limits | `13-permission-and-consent.md`, `04-sandboxed-execution.md`, `07-cost-guardrails.md` |
| Cost / Billing | Spend metering, hard budget caps | `07-cost-guardrails.md` |

## How to use this skill

### Building a new agent from scratch

**The user says one sentence. You do the rest.**

Nobody asking for an agent knows what reference 10 is, and they should never
have to. Route from the request's semantics, decide the defaults yourself, and
ask only about the things that are genuinely the user's call.

#### Decide these silently, from the request

| Question | How to answer it | Reference |
|---|---|---|
| **What may the agent produce?** | If the output has a format you can constrain — a script, a config, a scene, a query — design a small declarative vocabulary for it and forbid the raw format. If the agent must act on an open world (arbitrary files, arbitrary commands), keep the tool surface narrow instead and pay for it with permissions. | `10` |
| **What decides "done"?** | Is there a mechanical check? **Decisive** (compiler, simulator, schema validator, test suite that fully covers the goal) → *gating*: the run cannot end until it passes. **Partial** (linter, type checker, some tests) → *advisory*: report attribution-scoped, report-once, never block. **None** → the human decides, so invest in making the question cheap. Never zero authorities. | `03`, `13` |
| **Does it delegate?** | If a step reads far more than it writes, spawn a subagent for it. Allowlists replace, never extend. | `09` |
| **Will context outgrow the window?** | Only then: progressive disclosure, compaction ladder, memory. Not before. | `11`, `05`, `14` |

#### Wire these in every time, without being asked

These are not features to be requested. An agent missing any of them is
broken, and the user has no way to know that:

- **A sandbox** for anything the model authors and you then execute — OS-level
  boundary, network off, non-root, wall-clock kill. `04`
- **A hard budget cap**, checked before and after each call, plus a turn cap.
  If the domain has a second cost axis (GPU time, API quota, physical
  actuation), cap that too and estimate it *before* the call. `07`
- **Tool errors as data**, never exceptions. `02`
- **A typed loop**: named exits, named continuations, recovery ladders. `01`
- **Staging then promote**, so the durable store only ever holds verified
  work, and a crash leaves something inspectable. `08`
- **A consent ladder** the moment a human is in the loop at all. `13`

#### Ask the user only about these

Everything above you decide. These change what you build and cannot be
inferred:

1. **Is a human present, or is this headless?** Determines whether consent can
   be an authority at all.
2. **Does it compose with an existing agent or service?** (Naming one — "use
   Articraft for the assets" — is the user's call, not yours to assume.)
3. **What does one run cost, at most?** In money and in wall-clock.

Ask them together, once, in one question. Do not interview.

#### Do not put an intent-rewriter in front of this skill

A recurring instinct is to add a preprocessing layer that "improves" the
user's request before the skill sees it. Resist it, and note *which* request
we are talking about, because the two cases have opposite answers.

**"Build me an agent that X" needs no expansion.** The routing above extracts
a handful of bits from it — is the output constrainable, is there a mechanical
check, does a step read far more than it writes. A one-line request already
carries those. A rewriter in front can only lose them, and it becomes a second
place where intent disappears with nothing in the transcript to show it.

**"Build me a small study with a desk" absolutely needs expansion** — how big,
which wall, what height. But that is the *generated agent's* job, not a layer
in front of this one. Done there it has somewhere to live: the planning phase
resolves what it can, asks once about what it cannot, and writes the result
into the action space where the user can read and edit it. Done in front of
the skill, the same expansion is an invisible guess that nobody can audit.

The rule: **the skill's input needs no optimising; the built agent's input
does — and handling it is a feature of that agent, not a stage before this
one.**

#### Before writing anything, declare what you decided

Show this first, every time. It costs one screen and it is the only point at
which a misroute is cheap to fix — everything after it is code:

```
Building: <one line: what this agent does>

  Action space   <what it may produce>            <- because <reason>
  Verifier       gating | advisory | human        <- because <reason>
  Delegation     <yes, to what | no>              <- because <reason>
  Sandbox        <boundary>                       <- always
  Budget         <axes and caps>                  <- always
  Persistence    staging -> promote, traces       <- always
  Not yet        <what you are deliberately skipping and why>

Say if any of that is wrong. Otherwise I'll start.
```

Three reasons this earns its screen, and none of them is politeness:

- **A wrong decision surfaces on the first screen rather than after two hours
  of code.** The action space and the verifier tier are the two choices that
  cannot be repaired later without rewriting everything downstream.
- **Every line carries its reason**, so someone who has never heard of these
  references can still tell you that physics is not actually decisive in their
  domain, or that there is no human in the loop.
- **It is testable.** Same request twice should produce the same declaration.
  Routing that cannot be checked is routing you are trusting on faith.

What this is *not*: a request for permission to continue, and not an
interview. State the decisions, ask the one combined question from above if
you still need it, and then build. A reader who says nothing has agreed.

#### Then build in this order

The order is a dependency chain, not a checklist: the action space determines
whether a verifier is possible, and the verifier determines how the loop can
exit. Built backwards, both get rewritten.

1. **Action space first** (`10-action-space-sdk.md`): decide what the agent is
   allowed to produce and through which constrained vocabulary. The single
   highest-leverage design decision.
2. **Verifier second** (`03-evaluator-verifier.md`): define what "correct"
   means and how failures become structured, actionable feedback. Decide
   explicitly whether it **gates** (refuses the exit) or **advises** (reports
   without blocking) — most domains have real checkers but no oracle for
   "done", and that middle tier has its own rules. An agent with neither is a
   text generator.
3. **Tools** (`02-tools.md`): the smallest tool surface that lets the model
   act, with errors returned as data, never raised.
4. **The loop** (`01-agent-loop.md`): turn lifecycle, the fresh-verify success
   gate, no-action escalation, guidance injection.
5. **Prompts** (`06-prompts.md`): prompt-as-code, per-provider variants,
   docs in the first user message (not the system prompt).
6. **Provider layer** (`05-providers.md`): only if you need >1 LLM backend;
   otherwise keep a thin seam you can widen later.
7. **Guardrails & cost** (`13-permission-and-consent.md`,
   `07-cost-guardrails.md`, `04-sandboxed-execution.md`): the consent ladder if
   a human is in the loop, a hard budget cap, max turns, and an OS-isolated
   sandbox backend for executing generated code. Isolation is what buys
   autonomy — every class of action you can safely sandbox is a class of
   question you never have to ask.
8. **Persistence** (`08-state-persistence.md`): staging-then-promote, traces,
   provenance. Do this before you scale up runs — trajectories are the asset.
9. **Orchestration** (`09-orchestration.md`): CLI/entry points, derived runs
   (fork/edit/rerun), batch execution.
10. **Progressive disclosure** (`11-skills-progressive-disclosure.md`): only
    once the capability surface outgrows the context window — skills, deferred
    tool schemas, and the rule that nothing volatile may live in a cached
    prefix.
11. **Extension points** (`12-hooks-and-extension.md`): only once other people
    need to change your agent's behaviour without forking it.
12. **Planning** (`15-planning.md`): once tasks are ambiguous enough that
    building the wrong thing efficiently is a real risk. Planning is a
    permission mode, not a prompt.
13. **Memory** (`14-memory.md`): last, and only with a written rule for what
    belongs. A memory store that saves what `grep` could rediscover is worse
    than none.

### Building one component only

Jump straight to the reference. Every reference is self-contained: why the
module exists → how the case-study agent implements it (with `file:line`
provenance) → design-decision table → constants table → a generic
`Reusable pattern` code block → pitfalls → checklist.

### Starting from code

`templates/agentkit/` is a **working agent core**, not a skeleton: tool
contract, dispatch gauntlet, permission ladder, hooks, result overflow, skill
loader, typed-transition loop, and ten working tools (Read, Write, Edit, Glob,
Grep, TodoWrite, AskUserQuestion, Shell, ToolSearch, Skill). Stdlib only, no
dependencies. Verify it before trusting it:

```bash
python3 templates/agentkit/selftest.py     # -> selftest: 10/10 scenarios OK
```

Read `selftest.py:build_agent` first — it is the shortest honest example of
wiring the pieces together. Then copy the directory, delete the tools you do
not need, and add your domain's.

`templates/*.py` (single files) are the older per-pattern skeletons distilled
from Articraft — the verifier, provider seam, cost meter and sandbox contract.
Use them when you want one subsystem's shape without the kit.

## The five load-bearing invariants

If you take nothing else, take these. Each is tagged with **why you should
believe it**, because the strength genuinely differs and a reader deserves to
calibrate rather than take five equally confident assertions on faith:

- *mechanical* — follows from the API or OS contract, not from observation.
  Ignoring it produces a specific, reproducible failure.
- *converged* — both distilled agents arrived at it independently. Strong,
  but n=2 from one ecosystem; treat as a very good default, not a law.
- *single-source* — one agent does this and it is well-argued. Worth stealing,
  worth questioning in your domain.

1. **Success is verified, never self-reported.** *(converged, with a caveat —
   see the section below.)* Gate every "I'm done" on a
   fresh external check of the *latest* artifact revision (revision counter
   bumped on every mutation, compared against the revision at last successful
   verify). Models confidently declare victory over broken work.
2. **Tool errors are data, not exceptions.** *(mechanical.)* Every malformed call, validation
   failure, or runtime error goes back to the model as a structured tool
   result it can self-correct from. A raised exception aborts the loop and
   leaves a dangling `tool_call_id`.
3. **One primary issue at a time.** *(single-source: Articraft.)* When
   verification produces N failures, pick one root cause by a priority ladder
   (runtime error before policy violations before QC heuristics) and lead with
   it, because models given N co-equal failures patch the easiest rather than
   the causal one. Claude Code does **not** do this — its advisory verifier
   reports every new finding at once, relying on attribution-scoping to keep
   the list short (reference 03). Both work; which you need depends on whether
   your failures are usually one cause with many symptoms (ladder) or many
   independent problems (report all).
4. **Budgets are enforced in the loop, not hoped for.** *(mechanical.)* Hard USD cap checked
   both before the LLM call and after usage recording; max-turns counts
   no-action turns too; every terminal path persists the cost ledger.
5. **Never execute generated code without an isolated sandbox.** *(mechanical:
   a security property, not an empirical finding.)* Process
   timeouts and rlimits improve *reliability* but provide no security
   boundary — a child process is killable isolation, not a sandbox. Run
   model-authored code inside an OS-level boundary (container with
   networking disabled, microVM, gVisor, or a remote sandbox service),
   supervise it with a parent-enforced wall-clock kill, and return every
   failure in-band as typed data.

### When invariant 1 has no compiler

Invariant 1 assumes a mechanical check exists. For open-ended work — "improve
this code", "write this document" — it does not, and pretending otherwise
produces a verifier that measures nothing. The rule generalises to:

> **Exactly one authority must be able to say "no" to a finished attempt.**
> If the domain admits mechanical verification, that authority is the
> verifier (`03`). If it does not, it is the human — and the engineering then
> goes into making the question you ask them cheap, scoped, and rare:
> per-input permission predicates, structured multiple-choice questions, an
> approval gate modelled as a state transition, and an OS sandbox whose whole
> purpose is to *reduce* how often you must ask (`02`, `04`, case study 02).

What is never acceptable is *zero* authorities — the model finishing on its
own say-so with nothing external able to refuse.

## Dependencies

None. Templates are stdlib-only Python 3.10+ and run without installation.
Two capabilities are deliberately left as Protocols with refusing defaults —
executing generated code (`SandboxBackend`) and executing hooks
(`HookExecutor`) — because supplying an OS-level boundary is a deployment
decision this package will not make for you.

## Case studies

| Agent | Domain | What it contributes |
|---|---|---|
| [Articraft](case-studies/articraft.md) | text/image → articulated 3D assets (CadQuery → URDF) | References 00–10; the compile-feedback verifier pattern; SDK-shaped action space |
| [Claude Code](case-studies/claude-code.md) | general-purpose coding agent, human in the loop | References 11-13; the typed-transition loop and recovery ladders; the tool-call gauntlet and fail-closed tool contract; the consent ladder; the five-layer context strategy; `templates/agentkit/`; the [tool catalogue](case-studies/claude-code-tool-catalog.md) |

The library is designed to grow: each newly distilled agent adds a case study
and enriches the references where its design differs. See
`case-studies/README.md` for the distillation procedure.

## Common mistakes

- **Skipping the verifier.** If the agent's output cannot be checked
  mechanically, redesign the action space until it can (that is what
  `10-action-space-sdk.md` is for).
- **Letting the model author its own safety checks only.** Run a
  compiler/harness-owned baseline QC battery regardless of what the model's
  self-tests do; dedupe if it already ran the same checks.
- **Treating the anatomy as a checklist of mandatory boxes.** Small agents
  legitimately skip Memory, multi-provider, and batch orchestration. Never
  skip the verifier, the cost cap, or OS-level isolation of generated code.
