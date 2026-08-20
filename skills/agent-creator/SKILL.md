---
name: agent-creator
description: >-
  Design knowledge for building production LLM agents, distilled from real
  agent codebases (currently: Articraft, an agentic 3D-asset generator). Use
  when designing or implementing any part of an agent — the turn loop, tool
  layer, verifier/evaluator, multi-provider abstraction, system prompts, cost
  guardrails, state/trace persistence, sandboxed execution, or orchestration.
  Also use when reviewing an agent architecture or debugging agent failure
  modes (reward hacking, silent loops, runaway spend, context blowup).
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
| Planner | Decompose goals into steps | `01-agent-loop.md`, `09-orchestration.md` |
| Executor | Run each step, dispatch tools | `01-agent-loop.md` |
| State / Context | Where the task is, what tools returned | `08-state-persistence.md` |
| Memory | Cross-task knowledge, retrieval | `08-state-persistence.md`, `02-tools.md` (BM25 examples) |
| Skills | Reusable specialised abilities | `10-action-space-sdk.md` |
| Evaluator / Verifier | Is the result correct? Is the task done? | `03-evaluator-verifier.md` |
| Guardrails | Permissions, safety, limits | `04-sandboxed-execution.md`, `07-cost-guardrails.md` |
| Cost / Billing | Spend metering, hard budget caps | `07-cost-guardrails.md` |

## How to use this skill

### Building a new agent from scratch

Work through the modules in this order — each layer depends on the previous:

1. **Action space first** (`10-action-space-sdk.md`): decide what the agent is
   allowed to produce and through which constrained vocabulary. The single
   highest-leverage design decision.
2. **Verifier second** (`03-evaluator-verifier.md`): define what "correct"
   means and how failures become structured, actionable feedback. An agent
   without a verifier is a text generator.
3. **Tools** (`02-tools.md`): the smallest tool surface that lets the model
   act, with errors returned as data, never raised.
4. **The loop** (`01-agent-loop.md`): turn lifecycle, the fresh-verify success
   gate, no-action escalation, guidance injection.
5. **Prompts** (`06-prompts.md`): prompt-as-code, per-provider variants,
   docs in the first user message (not the system prompt).
6. **Provider layer** (`05-providers.md`): only if you need >1 LLM backend;
   otherwise keep a thin seam you can widen later.
7. **Guardrails & cost** (`07-cost-guardrails.md`, `04-sandboxed-execution.md`):
   hard budget cap, max turns, subprocess isolation for generated code.
8. **Persistence** (`08-state-persistence.md`): staging-then-promote, traces,
   provenance. Do this before you scale up runs — trajectories are the asset.
9. **Orchestration** (`09-orchestration.md`): CLI/entry points, derived runs
   (fork/edit/rerun), batch execution.

### Building one component only

Jump straight to the reference. Every reference is self-contained: why the
module exists → how the case-study agent implements it (with `file:line`
provenance) → design-decision table → constants table → a generic
`Reusable pattern` code block → pitfalls → checklist.

### Starting from code

`templates/` contains stdlib-only Python skeletons distilled from the case
studies: `agent_loop.py`, `tools.py`, `verifier.py`, `provider_adapter.py`,
`cost_meter.py`, `sandbox_runner.py`. They are starting points, not a
framework — copy, rename, and specialise.

## The five load-bearing invariants

If you take nothing else, take these — every one exists because its absence
is a known production failure mode:

1. **Success is verified, never self-reported.** Gate every "I'm done" on a
   fresh external check of the *latest* artifact revision (revision counter
   bumped on every mutation, compared against the revision at last successful
   verify). Models confidently declare victory over broken work.
2. **Tool errors are data, not exceptions.** Every malformed call, validation
   failure, or runtime error goes back to the model as a structured tool
   result it can self-correct from. A raised exception aborts the loop and
   leaves a dangling `tool_call_id`.
3. **One primary issue at a time.** When verification produces N failures,
   pick exactly one root cause by a priority ladder (runtime error before
   policy violations before QC heuristics) and lead with it. Models given N
   co-equal failures patch the easiest, not the causal one.
4. **Budgets are enforced in the loop, not hoped for.** Hard USD cap checked
   both before the LLM call and after usage recording; max-turns counts
   no-action turns too; every terminal path persists the cost ledger.
5. **Never trust generated code with your process.** Execute it in a spawned
   subprocess with a wall-clock kill, structured JSON result channel, and
   errors returned in-band.

## Dependencies

None (instruction + template skill; templates are stdlib-only Python).

## Case studies

| Agent | Domain | What it contributes |
|---|---|---|
| [Articraft](case-studies/articraft.md) | text/image → articulated 3D assets (CadQuery → URDF) | All 10 module references; the compile-feedback verifier pattern; SDK-shaped action space |

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
  skip the verifier, the cost cap, or sandboxing of generated code.
