# Case study 01: Articraft

**Domain:** text/image prompt → articulated 3D asset (CadQuery Python → URDF + meshes)
**Source:** https://github.com/articraftresearch/Articraft (Apache-2.0) · paper arXiv:2605.15187
**Distilled:** 2026-08 from ~20k lines of agent code (`agent/`, `sdk/`, `cli/`, `storage/`)

## What this agent does

Given "Create a realistic articulated desk lamp…", the agent writes a Python
script against a constrained SDK (declarative parts + articulations +
materials + self-tests), compiles it to URDF with derived collision geometry,
runs a two-layer QC battery, and iterates on structured compile feedback until
the latest revision passes — typically 10–40 turns, $0.5–$2 per asset.

Why it is worth distilling: it is a complete production implementation of the
hardest agent pattern — **generate → verify → self-correct over a rich
artifact** — with real answers for cost control, multi-provider support,
sandboxing, and trajectory persistence.

## Architecture

```
cli/main.py ──► agent/runner.py ──► single_run.py (staging dir, run.json "running")
                                        │
                                        ▼
                    agent/harness.py — the turn loop
                    │  per turn: cost gate → LLM call → tool dispatch
                    │  guidance injection → compile-feedback append
                    ▼
   tools: write_code/replace/apply_patch · read_file · find_examples (BM25)
          compile_model (intercepted by harness) · probe_model (subprocess)
                    │
                    ▼
   agent/compiler.py + sdk/ — execute model.py in subprocess (300 s cap),
   derive collisions from visuals, run agent tests + baseline QC
                    │
                    ▼
   agent/feedback.py — classify outcomes → CompileSignalBundle
   (severity failure/warning/note, ONE primary issue via priority ladder,
    per-failure-family playbooks, streak escalation at ≥3)
                    │
                    ▼ (only on fresh verified success)
   record_persistence.py — promote staging → records/ (immutable revisions)
   + cache/record_materialization/ (regenerable URDF+meshes)
   + trajectory.jsonl.zst + provenance.json + cost.json
```

## The numbers that matter (production-tuned constants)

| Guardrail | Value |
|---|---|
| Max turns | 100 (250 for Gemini 3 Flash — cheaper per turn) |
| No-action handling | nudge → hard escalation at streak 2 → abort at 3, only if escalation was delivered |
| Compile timeout | 300 s subprocess kill (env-tunable, 0 disables isolation) |
| Probe timeout | 600 s default, 100 ms floor |
| Cost cap | CLI arg > record value > `ARTICRAFT_MAX_COST_USD` env; checked pre-call AND post-usage |
| Retry policy | 4 attempts, full-jitter 0.5 s→20 s, 900 s request timeout; Gemini: 7 attempts, 300 s |
| Compaction triggers | hard at 0.90 pressure; soft bands (0.85/streak 3, 0.70/4, 0.55/5); +1 streak required when cache ratio ≥ 0.60; 2-turn cooldown |
| Trace compression | zstd level 19 at commit; JSONL flush-per-event during run |

## The ten design moves worth stealing

1. **Success gate on a revision counter** (`harness.py:1090-1149`). Every
   successful mutating tool bumps an edit-revision integer; finish attempts
   compare it against the revision at last successful compile. Stale → inject
   `<compile_required>`, continue. Unfakeable, one integer.

2. **One primary issue via a priority ladder** (`feedback.py`). Runtime error
   (0) < structural policy (1) < compiler-owned QC (2) < stale contracts (3)
   < authored-test failures (7) < default (90). The head becomes "Primary
   issue"; playbooks escalate from patching to diagnosing at failure streak ≥3.

3. **Two-layer verification.** The model authors `run_tests()` (exact,
   prompt-specific contracts: `expect_contact`, `expect_overlap`) but the
   compiler always runs its own baseline QC battery regardless, deduping
   checks the model already ran. Trust but verify — the model cannot be
   relied on to include the safety net.

4. **Justified allowances instead of blanket exceptions.** Real mechanisms
   need overlaps (hinge pins in barrels), so `allow_overlap(a, b, reason=…)`
   demands a non-empty reason, scopes to named elements, echoes into the
   report, and still warns. Tolerances are clamped (joint-origin cap 0.15 m)
   so the model cannot neutralise checks with huge values.

5. **Derived collision geometry.** Agents may not author collisions at all;
   the compiler derives them 1:1 from visuals. Eliminates the visual/physics
   desync silent-error class entirely.

6. **Per-provider tool idioms + prompt build matrix.** OpenAI gets
   grammar-constrained freeform `apply_patch`; Gemini gets `write_file` with
   `content`/`path` (its RL-trained shape) aliasing the canonical tool.
   Consequently system prompts are compiled per provider from shared markdown
   sections — 6 artifacts, checked in, staleness-gated by tests.

7. **Native-format echo through a neutral envelope.** Providers keep
   canonical history in native format; the harness sees normalized dicts with
   `extra_content['<provider>']` carrying thinking blocks / thought
   signatures (base64 for bytes) for lossless round-trip. Server-side state
   is treated as a lease: `previous_response_not_found` triggers full resend
   from local history.

8. **Domain signal drives compaction.** Consecutive compile-failure count
   feeds the compaction policy: being stuck justifies summarising earlier.
   The immutable prefix (task + SDK docs, also the cache anchor) and the raw
   tail are never compacted.

9. **Staging-then-promote with provenance.** The library only ever contains
   verified, compilable assets; failures keep their staging dir and get a
   `results.jsonl` row + distinct exit codes (2 agent / 3 compile / 4
   persist). `provenance.json` pins git commit, uv.lock sha, system-prompt
   sha, model/settings — any asset can be re-run or forked months later.

10. **The runner re-verifies out-of-band** (`single_run.py:420-444`). Even
    after the agent claims success, if no checkpoint URDF exists the runner
    recompiles the final script itself before persisting. Never trust the
    agent's success claim without an artifact.

## Failure modes this design guards against

| Failure mode | Guard |
|---|---|
| Success-by-assertion | Fresh-verify gate + out-of-band recompile |
| Reward hacking (deleting geometry to pass checks, blanket allowances) | Anti-reward-hacking prompt language, justified+scoped allowances, tolerance clamps, derived collisions |
| Patching the easiest of N failures | Single primary issue, priority ladder |
| Same-failure loops | Failure signatures, streak escalation to probe-first playbooks, compaction on plateau |
| Silent/empty responses looping forever | No-action turns consume turn budget; two-stage escalation with provider diagnostics |
| Runaway spend | Dual-checkpoint USD cap incl. compaction spend; cost.json persisted on every terminal path |
| Generated-code hangs/crashes | Spawned subprocess + wall-clock kill for compile and probe |
| Context blowup | Pressure+streak compaction; duplicate retrieval content replaced by a sentinel |
| Torn/corrupt library state | Staging-then-promote; append-only revisions; regenerable materialization cache |

## Known sharp edges (inherited, do better)

- Unknown (provider, model) pricing silently disables the cost tracker — a
  user-set cap is then never enforced. Fail loudly instead.
- No memory rlimit on subprocesses (only time limits); the probe namespace is
  not an import/filesystem sandbox — isolation relies on process boundaries.
- Repeat-failure detection hashes the whole signal bundle: cosmetic changes
  (a different measured distance) defeat streak detection.
- Plain `write_json` (no temp-file+rename) can tear on crash; the design
  leans on staging-then-promote to contain it.
- `apply_patch` skips the model-contract AST check that `write_code`
  enforces — a patch can strip required entrypoints undetected until compile.

## What transfers to your domain

Replace "CadQuery script" with any rich artifact (SQL migration, Terraform
plan, video-editing EDL, protein design) and the machinery is unchanged:
constrained authoring vocabulary → subprocess verify → typed signals → one
primary issue → revision-gated success → staged persistence. The domain
specifics live in exactly two places: the SDK (action space, reference
`10`) and the QC battery (verifier, reference `03`). Everything else is
this skill's templates.
