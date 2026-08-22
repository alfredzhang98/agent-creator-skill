# Templates

Stdlib-only Python, no dependencies. Two layers:

**`agentkit/` — a working agent core.** Not a skeleton: every file runs, and
`python3 agentkit/tests.py` runs 174 regression assertions and
`agentkit/selftest.py` is the worked example. This is the harness distilled from Claude Code 2.1.88 — tool
contract, dispatch gauntlet, permission ladder, hooks, result overflow, skill
loader, typed-transition loop — plus working Read/Write/Edit/Glob/Grep/Todo/
AskUser/Shell/ToolSearch/Skill implementations. **Start here if you are
building an agent.** See [`agentkit/README.md`](agentkit/README.md).

**The single-file skeletons below — one pattern each.** Distilled from
Articraft (`Articraft/src/articraft/`), domain-neutral, meant to be read and
adapted rather than imported. Use them when you want the shape of one
subsystem without the rest of the kit.

| File | What it gives you | Reference doc |
| --- | --- | --- |
| `agent_loop.py` | The turn loop: dual counters (turns vs llm_calls), two-stage no-action escalation then abort, finish gate requiring a fresh verify, cost-cap checks before and after each LLM call, tool errors returned as messages | `references/01-agent-loop.md` |
| `tools.py` | Declarative tool base: param specs, JSON-schema generation, build/execute invocation lifecycle, resource binding, registry + dispatch, every failure as a `ToolResult` | `references/02-tools.md` |
| `verifier.py` | Structured signal vocabulary (failure/warning/note), bundle rendering for the LLM with primary-issue selection and response rules, revision-keyed verify cache with repeated-failure streaks | `references/03-evaluator-verifier.md` |
| `sandbox_backend.py` | The execution *contract* for generated code — `SandboxPolicy` (minimum security bar as data), the `SandboxBackend` interface you implement per deployment, a fail-closed default, and the defensive result-parsing ladder. Ships no execution engine: bring your own OS-level boundary (container / microVM / gVisor / remote service) | `references/04-sandboxed-execution.md` |
| `provider_adapter.py` | Minimal provider seam: `complete(messages, tools) -> {text, tool_calls, usage}`, codec protocol, env key rotation, thinking-level mapping table, jittered retries, dry-run payload preview | `references/05-providers.md` |
| `cost_meter.py` | Pricing table (cached vs uncached input, cache writes, output), per-turn accumulation, separate maintenance ledger, hard cap that aborts BEFORE the next call, budget override cascade | `references/07-cost-guardrails.md` |

## agentkit at a glance

| File | What it gives you | Reference |
| --- | --- | --- |
| `agentkit/contract.py` | The `Tool` declaration: per-input safety predicates, fail-closed defaults in one `build_tool`, errors-as-data results, per-tool result caps | `02` |
| `agentkit/registry.py` | Pool assembly, deny-filter-before-assembly, cache-stable ordering, deferred schemas + search query language | `02`, `11` |
| `agentkit/pipeline.py` | The seven-stage gauntlet between a `tool_use` block and the world; opt-in parallelism; never raises | `02`, `13` |
| `agentkit/permissions.py` | The consent ladder (1a→3), scoped rules, bypass-immune classes, unknown-means-ask | `13` |
| `agentkit/hooks.py` | 27-event lifecycle, exit-code + JSON protocol, strictness-ordered aggregation, refusing default executor | `12` |
| `agentkit/result_store.py` | Overflow-to-disk with bounded previews; per-result and per-message budgets | `02`, `08` |
| `agentkit/skill_acquisition.py` | Buy before you build: registry queries, two-gate trust (author, then prose), plans instead of subprocesses, content pins | `16` |
| `agentkit/skills_loader.py` | Progressive disclosure: frontmatter, realpath dedup, conditional activation, budgeted index | `11` |
| `agentkit/loop.py` | Turn loop as a typed state machine: closed `Stop`/`Continue` sets, recovery ladders, in-loop budgets | `01` |
| `agentkit/verifier.py` | Advisory verification: attribution-scoped baseline diff, report-once | `03` |
| `agentkit/state.py` | Transcript, metadata, staging→promote, tolerant readback + orphan repair | `08` |
| `agentkit/memory.py` | Non-derivability rule, manifest → cheap-model recall, staleness, write validation | `14` |
| `agentkit/planner.py` | Plan mode phase machine, guarded transitions, durable plan file | `15` |
| `agentkit/preflight.py` | Model ID resolved against the live catalogue, never from memory; ambiguous families refused; key/capability/ping checks before turn one | `05` |
| `agentkit/provider.py` | Backend seam: caller-aware retry, usage normalisation, pure compaction decisions | `05` |
| `agentkit/prompts.py` | Build matrix, memoised sections, declared cache boundary | `06` |
| `agentkit/orchestration.py` | Staged exit codes, staging→promote, fork/rerun, declarative subagents | `09` |
| `agentkit/tools/` | Read · Write · Edit · Patch · Glob · Grep · TodoWrite · AskUserQuestion · Shell · ToolSearch · Skill · Delegate | `02`, `11`, `13` |
| `agentkit/selftest.py` | Worked example: builds an agent and drives it | — |
| `agentkit/tests.py` | 257 regression assertions, named for the defects they keep fixed | — |

Not templated (see the reference docs directly): prompts-as-code
(`06-prompts.md`), state persistence (`08-state-persistence.md`),
run orchestration (`09-orchestration.md`), and the typed action-space SDK
(`10-action-space-sdk.md`).

## What neither layer ships

No `exec`, `eval`, `compile`, `subprocess`, or process spawn anywhere. The two
execution surfaces — running generated code, and running hooks — are Protocols
(`sandbox_backend.py`, `agentkit/hooks.py`) whose defaults refuse with an
actionable message. Supplying an OS-level boundary is a deployment decision,
and this package makes you make it.
