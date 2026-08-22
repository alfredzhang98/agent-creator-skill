# Independent review, 2026-08

Five reviewers audited `templates/agentkit/` and the references they implement,
each taking three or four modules with instructions to be specific and to find
defects rather than summarise. Every claim below was then **reproduced by
executing the code** before being accepted; the ones that did not reproduce are
listed at the end.

The result changed the project's own assessment of itself, so it is recorded
here rather than quietly fixed.

## The finding that mattered most

Not any individual bug. **The self-test stopped where the bugs started.**

The deferred-tool case asserted that `ToolSearch` returned a schema and never
asserted that the subsequent call succeeded — and it could not have, because
nothing removed the tool from the withheld set. The model fetched a schema,
called the tool, was told to fetch the schema, and looped until the turn budget
ran out. The flagship feature had never worked, and a 14/14 green suite said
otherwise.

Fourteen self-written tests passed while five reviewers found 33 reproducible
defects in the same code. That gap is the lesson: a test suite written by the
author of the code, shaped by the same assumptions, measures agreement rather
than correctness.

## What was found, and fixed

### Could not work at all

| Defect | Fix |
|---|---|
| Deferred tools were permanently uncallable — `Pool.deferred` was immutable and `ToolSearch` never cleared it | `Pool.loaded` mutable set; `is_withheld()` gates dispatch; `ToolSearch` marks fetched tools loaded |
| `announce_deferred()` was defined and never called — the model was never told which tools existed but were unloaded, so it had no reason to search | The loop injects it once per run |
| `search("+slack")` always returned empty; required-only queries scored zero | Required terms now contribute to the score |
| `to_api_schema(defer=True)` dropped `input_schema`, which is a 400 against the real API | Deferred tools send the full definition plus `defer_loading`; the server withholds it |

### Security inversions

| Defect | Fix |
|---|---|
| An invented ladder step converted `ask` → **allow** in `dont_ask` mode; the source converts `ask` → **deny** | Two-layer design: the ladder decides on merits, an outer pass applies mode transformations that no early return can skip |
| A permission predicate that raised produced **allow** | Raising predicates fail closed at every mode |
| `accept_edits` was inverted — read-only tools asked, writers were allowed | Grants apply to an explicit `edit_tools` allowlist, never to destructive calls |
| Bypass immunity keyed on `"safety"` while the source emits `"safetyCheck"`, so a faithful port lost immunity | Both spellings honoured; unknown reason types now raise rather than silently losing the guarantee |
| A content-scoped deny with no rule key evaporated into `allow` | An unevaluable deny becomes `ask`, never `allow` |
| Rule content ran through `fnmatch`, so a hand-typed `a?b` silently became a glob | Exact and `prefix:*` only |
| `Phase.ABANDONED` was not read-only — abandoning a plan granted full write access with nothing approved | Only `EXECUTE` unlocks writes |
| Plan approval bound to a boolean, so the plan could be rewritten after sign-off | Approval binds to a content digest, re-checked on execute |
| `persist()` joined an untrusted `tool_use_id` into a path; `"../../x"` wrote outside the results directory | Sanitised and hash-suffixed stem |

### Silently wrong

| Defect | Fix |
|---|---|
| A checker returning nothing wiped the verifier baseline, so every pre-existing problem was reported as newly caused | `CheckResult` distinguishes clean / partial / unavailable; the baseline advances only for paths actually checked |
| The diff key collapsed repeated messages, hiding second occurrences | Baseline is a multiset |
| Verifier path identity was a raw string; an absolute/relative mismatch discarded every finding | Paths normalised on both sides |
| LSP-style `"Error"` raised `KeyError`; `normalize_severity(True)` read as severity 1; `"Information"` became `error` | Normalised on construction, with an alias table and a bool guard |
| `before_edit(path)` defaulted to an empty baseline — the usage example demonstrated the exact failure the class prevents | The baseline argument is required |
| `memory.select()` used a `set`, making recall nondeterministic across processes and discarding the model's ranking | Order-preserving, keyed on filename (unique) rather than frontmatter name |
| The selector prompt asked for filenames while the manifest printed names — a compliant model matched nothing | Both use the filename |
| The relative-date check missed every weekday, "next sprint", "in 2 weeks" | Rewritten, and it now skips code spans |
| `apply_message_budget` could make a message **larger** and never converged | Swaps only when the replacement is smaller; reports whether the budget was met |
| `drop_orphan_tool_calls` left orphan `tool_result` blocks, ignored Anthropic-shaped `tool_use`, and accepted a result preceding its call | Three-pass repair over both dialects, order-aware |
| Stop hooks fired twice on every completion, and fired at all on API-error exits | Once, at the finish gate; error exits emit `StopFailure` |
| The cost cap was checked before the call but not after, so a single overspending turn returned `COMPLETED` | Checked on both sides |
| The output-truncation "escalate" rung re-sent an identical request | The `Model` protocol carries `max_output_tokens` |
| An unpriced model silently disabled the meter, so a configured cap never tripped | Refuses to start, or warns on explicit opt-in |
| A blank `AGENT_MAX_COST_USD` crashed at startup | Blank means unset |
| Block-style YAML `paths:` made a conditional skill unconditional | Block lists are parsed |
| `fnmatch` was documented as gitignore semantics but differs in both directions | Real gitignore matching |
| `Skill` was a frozen dataclass containing a dict — hashing raised at runtime | `eq=False` |
| `hooks.SCHEMA_HINT` documented a nested shape the parser could not read, so a correctly-written deny was discarded | Flat `hookEventName` shape, matching the parser and the source |
| A hook that denied could still rewrite the tool input | Rewrites accepted only from hooks that did not veto |
| A permission hook that timed out contributed nothing | Timeouts fail closed on permission events |
| `EVENTS` listed 17 of 27, so a hook following the reference crashed the runner | Complete, with per-purpose timeouts |
| Tool-result blocks carried an illegal `tool_name` key | Wire blocks carry API-legal fields only; exemption is by index |
| Error results bypassed the size cap | Capped too |

### Documentation errors

| Claim | Reality |
|---|---|
| ref 15: `requiresUserInteraction()` makes approval un-bypassable | It returns **false** for teammates — "exits locally without approval" |
| ref 15: the phase model | Built on the interview variant, which the source says is ~1% of traffic |
| ref 12: the trust gate has "no exceptions" | Non-interactive/SDK sessions run every hook |
| ref 12: 500 ms slow-hook threshold | 500 ms is a UI display threshold; the log threshold is 2,000 ms |
| ref 05: compaction summaries capped at 20k | A window reservation; the primary path explicitly refuses to set the cap |
| ref 05: post-compact restores 5 files totalling 50k | 5 files at 5k each, inside a 50k shared pool |
| ref 08: pitfall recommended opening the trace `"x"` | `"x"` raises on every resume — the case the section is about |
| ref 02: deferral above 10% of context | Deferral is on by default; 10% is the opt-in `auto` mode |
| ref 03: the checklist | Covered only the gating tier while the text recommends the advisory one |
| ref 13: "one ordered ladder" | Two layers; the missing outer one is what produced the `dont_ask` inversion |

## Where the reviewers were wrong

Recorded because a review is evidence, not authority:

- One reviewer reported the `~12,400`-line denominator for `src/tools/BashTool/`
  as wrong and gave `10,894`. That count omits `.tsx`; the actual total is
  **12,411** and the figure stood.
- One reported `Report.render` exceeding its character cap; it did not
  reproduce. The *ordering* defect in the same function did, and was fixed.

## What changed as a result

- `templates/agentkit/tests.py` — 120 assertions, one rule: **assert the
  outcome, not the intermediate step**. Each case is named for the defect it
  keeps fixed.
- `selftest.py` is now only the worked example, and asserts effects (the edit
  reached disk; the denied write did not).
- `tools/verify_citations.py` gained content anchors, so a citation whose
  target was refactored away reports `DRIFTED` instead of passing.

## Follow-up: the unauditable half

A reviewer's sharpest structural criticism was that ~85% of the citation
surface pointed at Articraft, a tree absent from the repository — "the
document reads as authoritative while being unauditable." That is now
addressed: `verify_citations.py` covers both sources, and the anchor count
went from 364 to **946**.

Worth recording what the check found. Thirty-seven citations initially came
back broken, and **none of them was a wrong claim**. All three causes were
defects in the checker itself:

* it resolved bare filenames into `.venv`, so `base.py` matched 23 files and
  therefore matched none;
* when a public façade and its private implementation shared a basename it
  preferred the façade, while line-numbered citations point at the
  implementation;
* its citation regex required a leading letter, silently truncating
  `_shared.py` to a filename that does not exist.

A verification tool that reports false positives trains you to ignore it,
which is the same failure as a test suite that cannot fail. Both were found
in the same week.

## What this does not fix

The agentkit still has never run against a live model. Every claim here is
about behaviour under test. The two structural limits stated in the README —
n=2 distilled agents, one pinned source version — are unchanged.
