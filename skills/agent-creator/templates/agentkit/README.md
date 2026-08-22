# agentkit — a working agent core you can lift

Stdlib-only Python, no dependencies, no framework. Every file runs; the whole
thing is exercised by `selftest.py`:

```bash
python3 selftest.py   # worked example: wires an agent and drives it
python3 tests.py      # regression suite: 120 assertions, all green
```

The two are separate on purpose. `selftest.py` answers *how do I use this*;
`tests.py` answers *is it still correct*. Conflating them is how the previous
version came to pass 14/14 while an independent review found 33 defects — see
[`docs/AUDIT.md`](../../../../docs/AUDIT.md).

This is the *harness* layer distilled from Claude Code 2.1.88 and Articraft —
the part that is the same whatever your agent does. Copy the directory, delete
the tools you do not need, add the ones your domain needs, and keep the spine.

## What is here

| File | What it gives you | Reference |
|---|---|---|
| `contract.py` | The `Tool` declaration: per-input safety predicates, fail-closed defaults via `build_tool`, `ToolResult` with errors-as-data, per-tool result caps | `02-tools.md` |
| `registry.py` | Pool assembly: deny-filter before assembly, cache-stable ordering (built-ins as a contiguous prefix), deferred schemas + the search query language | `02`, `11` |
| `pipeline.py` | The seven-stage gauntlet between a `tool_use` block and the world; opt-in parallelism; never raises | `02`, `13` |
| `permissions.py` | The consent ladder (1a→3), rule matching with prefix/glob scopes, bypass-immune decision classes, unknown-means-ask | `13-permission-and-consent.md` |
| `hooks.py` | Lifecycle extension points: exit-code + JSON protocol, matcher filtering, strictness-ordered aggregation, refusing default executor | `12-hooks-and-extension.md` |
| `result_store.py` | Overflow-to-disk with a bounded preview; per-result and per-message budgets | `02`, `08` |
| `skills_loader.py` | Progressive disclosure: frontmatter parsing, realpath dedup, conditional activation, the budgeted index | `11` |
| `loop.py` | The turn loop as a typed state machine: closed `Stop`/`Continue` vocabularies, the recovery ladders, budgets enforced in-loop | `01-agent-loop.md` |
| `verifier.py` | Advisory verification: baseline-diff scoped by attribution, report-once, shared severity vocabulary | `03-evaluator-verifier.md` |
| `state.py` | Session state: append-only transcript, atomic metadata, staging→promote, tolerant readback with orphan repair | `08-state-persistence.md` |
| `memory.py` | Cross-session memory: non-derivability rule, header manifest, cheap-model recall, staleness, write validation | `14-memory.md` |
| `planner.py` | Plan mode as a phase machine: enforced read-only, guarded transitions, durable plan file, one-task-in-progress | `15-planning.md` |
| `tools/files.py` | Read / Write / Edit — line-anchored reads, uniqueness-or-error edits, read-before-write, staleness detection | `02` |
| `tools/search.py` | Glob / Grep — pruned walks, tight result caps, actionable empty results | `02` |
| `tools/control.py` | TodoWrite / AskUserQuestion — plan externalisation and structured questions | `06`, `09` |
| `tools/shell.py` | Shell — per-command read-only/destructive classification, rule keys, sandbox-gated execution | `04`, `13` |
| `tools/meta.py` | ToolSearch / Skill — the two tools that manage the agent's own surface | `11` |
| `selftest.py` | Worked example — read `build_agent` first, it is the whole integration surface |
| `tests.py` | Regression suite. One rule: assert the outcome, never the intermediate step |

## The 30-second version

```python
ctx      = Ctx(cwd=workdir, session_dir=workdir)      # what tools may see
pool     = registry.assemble([ReadTool, EditTool, ...], context_window_tokens=200_000)
perm_ctx = permissions.PermissionContext(mode="default")

result = loop.run(model, pool, ctx, perm_ctx,
                  [{"role": "user", "content": task}],
                  budget=loop.Budget(max_turns=40, max_usd=5.0),
                  ask=my_permission_dialog,       # returns True/False
                  compact=my_compaction,          # returns fewer messages, or None
                  cost_of=my_pricing)
result.stop          # Stop.COMPLETED | MAX_TURNS | COST_LIMIT | ...
result.transitions   # [Continue.NEXT_TURN, Continue.COMPACTED, ...]
```

`result.transitions` is the point: the loop's control flow is data, so a test
can assert *which* recovery path fired without parsing a transcript.

## What you must supply

Three seams, all deliberate — each is a decision only your deployment can make:

1. **A model.** Anything with
   `complete(messages, tools) -> {"text", "tool_calls", "usage", "error"}`.
   `error` is a *typed* string (`"context_too_long"`, `"output_truncated"`);
   the loop dispatches on it, so map your provider's errors onto that
   vocabulary rather than passing prose through.
2. **A `SandboxBackend`** (`../sandbox_backend.py`) if you want the shell tool
   to run anything. The default refuses with an actionable message. Supply a
   container with networking disabled, a microVM, gVisor, or a remote sandbox
   service — the classification in `tools/shell.py` is a *warning* layer, never
   the boundary.
3. **A checker** for `verifier.py` — whatever your domain actually has (type
   checker, linter, test runner, schema validator), wrapped to return
   `Signal`s. The module supplies the baseline diff; you supply the checker.
4. **A cheap model** for `memory.select` — recall is a small-model judgement
   over one-line descriptions, not an embedding lookup.
5. **A `HookExecutor`** if you want hooks. Hooks run commands from
   configuration files that may have arrived with a cloned repository, so this
   package ships no spawner and makes you place the trust gate yourself.

## What this package does and does not do

It **does** touch the filesystem: Read/Write/Edit/Glob/Grep are real
implementations, and `result_store.py` writes overflow files. That is the
point — these are the tools you were going to write anyway, with the
preconditions already in them.

It **does not** execute anything: there is no `exec`, `eval`, `compile`,
`subprocess`, or process spawn in any file. Both execution surfaces — running
shell commands and running hooks — are Protocols with refusing defaults. That
is not an omission; it is the position argued in `references/04`:

> A child process is a reliability boundary, not a security sandbox.

## Porting notes

- **Keep the fail-closed defaults in one function.** The single highest-value
  line in `contract.py` is that `build_tool` fills `is_read_only → False`. Move
  those defaults to the call sites and you will, eventually, ship a mutating
  tool that runs in parallel with an edit.
- **Keep `Stop`/`Continue` closed.** The moment a loop can exit for an unnamed
  reason, "why did this stop?" becomes archaeology.
- **Keep errors as data.** Every rejection in `pipeline.py` returns a
  `ToolResult`. Raise instead and you leave a `tool_use` block with no matching
  `tool_result`, and the *next* API call fails on a malformed conversation —
  far from the bug.
- **Do not widen `RESERVED_INPUT_FIELDS` into a blocklist.** It is defence in
  depth behind `additionalProperties: false`, not a substitute for it.
