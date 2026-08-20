# 09. Run Orchestration & Entry Points

**Maps to:** Executor · State/Context · Memory · Tools · Evaluator/Verifier · Cost · **Distilled from:** Articraft `agent/runner.py`, `agent/runner_cli.py`, `agent/single_run.py`, `agent/run_context.py`, `agent/edit.py`, `agent/rerun.py`, `agent/examples.py`, `agent/tools/find_examples.py`, `agent/tui/single_run.py`

## Why this module exists

The agent loop produces an artifact; something must own everything around that loop: parsing CLI flags, resolving settings from flag/env/default cascades, minting a per-run workspace, launching the harness, independently verifying the output, and promoting it into a durable record store with enough provenance to reproduce the run months later. Without this layer, every run is a one-off — you cannot fork a previous result, rerun it with a newer model, or tell from an exit code which stage failed. Orchestration is also where trust boundaries live: the runner never takes the agent's word for success, never deletes debug evidence on failure, and never spends API money before credentials and payloads are validated. Finally, it owns two derived-run flows (fork/edit and rerun-in-place) that turn a pile of runs into a versioned, lineage-tracked library.

## How Articraft implements it

### Composition root with function-level dependency injection

`agent/runner.py` contains no logic — it is a facade and composition root (`agent/runner.py:216-278`). Every real function in the lifecycle modules takes its collaborators as keyword-only parameters with production defaults: `execute_single_run_func`, `agent_cls`, `compile_report_func`, `write_success_record_func`, `resolve_record_author_func`. `runner.py` wires concrete implementations into these seams (`agent/runner.py:216-224` for the executor, `255-270` for edit/rerun, `273-278` for the CLI). The module docstring (`agent/runner.py:1-11`) documents the split: `single_run` owns the lifecycle, `record_persistence` owns writes, `edit` owns copy-edit, `rerun` owns reconstruction, `runner_cli` owns argument parsing. No DI framework — just keyword defaults, which makes every stage testable with fakes.

### CLI entry: layered resolution and pre-flight gates

`main()` resolves each setting through a strict cascade: flag > env > inference > default. Provider is inferred from the model id and `parser.error()`s when inference fails (`agent/runner_cli.py:75-91`); thinking level validates against an enum (`94-98`); budget falls back to an env-derived cap (`209-213`); data dir cascades flag > env var > repo-relative default (`38-44`). Two gates run before any API spend: `--dump-provider-payload` builds the exact turn-1 provider payload and exits 0 with no API call (`agent/runner_cli.py:253-281`), and `validate_provider_credentials` fails fast with exit 1 (`283-287`). Only then does `asyncio.run(run_from_input(...))` return the exit code (`289-310`).

### Deterministic per-run workspace context

`_build_single_run_context` mints all IDs and paths up front into one frozen dataclass (`agent/run_context.py:337-381`). IDs are timestamp + content hash, not UUIDs: `run_{utc_ts}_{sha1(prompt)[:8]}` and `rec_{slug[:48]}_{ts}_{digest}` (`348-352`) — human-scannable in a directory listing, time-sortable, collision-resistant. The staging dir is created eagerly and holds fixed-name working files both harness and runner know: `prompt.txt`, the source file the agent edits, the checkpoint artifact, `traces/`, `cost.json` (`355-371`). Record-side promotion paths are precomputed so promotion is a pure copy plan (`372-380`). Edit/rerun callers pass `record_id`/`revision_id` to extend an existing record.

### Lifecycle with staged failure exit codes

`execute_single_run` (`agent/single_run.py:191-562`) runs: build context → snapshot settings → **persist a `status='running'` row before the agent starts** so hard crashes leave a visible half-finished record (`332-359`) → run the agent as an async context manager, capturing `agent.llm.model_id` *after* the run because providers alias model ids (`364-388`). A local `_persist_failure` closure (`264-330`) writes the failed row (including the kept staging path) and returns a distinct exit code per stage: 2 agent runtime/reported failure (`389-406`), 3 final compile failure (`435-444`), 4 persistence failure (`495-504`). If the agent left a checkpoint-compiled artifact it is trusted; otherwise the runner independently recompiles the final script in a thread under a bounded concurrency slot (`420-434`) — success is never taken on the agent's word. Only the success branch deletes staging (`493-494`). All blocking I/O goes through `asyncio.to_thread`.

### Fork/edit: parent record becomes seeded context

`edit_record` loads the parent record plus its `provenance.json` (`agent/edit.py:169-225`) and resolves every setting explicit-arg > stored-provenance > env/default (`260-289`). It creates a NEW record at `rev_000001` with collision guards on both user-supplied and generated ids (`291-298`, `328-335`), then physically copies the parent's active source file into the new staging dir via `shutil.copy2` (`337-338`) so the agent starts from working code. Provenance threads through: `lineage={origin, parent_record, parent_revision, edit_mode:'copy'}` (`310-319`), `revision_seed` naming the seeded artifact (`346-351`), and `inherited_inputs` deduped by `(record_id, revision_id, path)` with sha256 digests — a content-addressed input chain across generations (`61-129`). Context is reconstructed purely from artifacts: `EDIT_RUNTIME_GUIDANCE` (`43-46`) tells the model the parent code is already staged, to make the smallest coherent change, and — critically — *not* to assume prior conversation history exists; this plus parent ids and the parent's original prompt is prepended to the first user turn.

### Rerun-in-place: settings reconstruction from provenance

`rerun_record_in_place` (`agent/rerun.py:56-214`) regenerates a record from its own stored artifacts: the active `prompt.txt` (mandatory, `81-85`), `provenance.json` (`87-102`), with CLI overrides layered on top (`109-159`). The thinking level is preferentially recovered from the original run's persisted parameters via `source.run_id` before falling back to provenance (`112-119`). The context reuses the SAME `record_id` but `revision_id=next_revision_id(...)` (`177-183`; `storage/revisions.py:31-41` scans `rev_NNNNNN` dirs and increments), so the rerun lands as a new revision, not a new record. Unlike edit, `revision_seed=None` (`212`): no code is copied; the model regenerates from the prompt alone. Conflating these two semantics corrupts history — a rerun that starts from existing code is an edit in disguise.

### Few-shot retrieval as an on-demand tool

The examples library (`agent/examples.py`) is a BM25 index over curated markdown with strict YAML frontmatter (hand-rolled parser at `364-410`, no YAML dependency). Each doc is decomposed into six fields — slug, title, tags, description, code identifiers, prose — and field weighting inside one flat index is achieved by **token repetition**: slug ×6 down to prose ×1 (`39-46`, `295-314`). Search retrieves a pool of `min(n_docs, max(limit*6, 12))` candidates (`706`), reranks with additive bonuses (exact/prefix/phrase per-field, alias hits at 0.3×, `coverage² × 3.0`, `466-569`), then applies hard precision gates: single-token queries drop code-only/prose-only hits below length and score floors (`594-601`); multi-token queries require metadata evidence for "strong" results and otherwise fall back to at most 2 results labeled `weakly_relevant` (`614-687`). Returning `[]` beats returning noise. `FindExamplesTool` (`agent/tools/find_examples.py:17-116`) exposes this to the model; its schema description (`77-89`) is prompt engineering — it coaches short concrete queries, states what the tool does NOT search, and warns that weak results are inspiration-only.

### Runtime guidance prepending

`build_initial_user_content` produces plain text or structured text+image parts, and `prepend_runtime_guidance` injects a guidance block BEFORE the user's prompt (`agent/tools/__init__.py:119-180`). This is the single seam through which edit runs get their "this is a fork" framing — the guidance lives in the first user turn, so the system prompt stays byte-identical and cacheable across fresh, edit, and rerun flows.

### Display-only observer with spinner-aware logging

`SingleRunDisplay` (`agent/tui/single_run.py:46-64`) is a pure observer: the harness calls `start_turn`/`end_turn`, `add_llm_call`, `add_tool_call`, `add_compile_result`, `add_thinking_summary`, etc., and the display prints cargo-build-style indented lines (`Turn 1/100`, `llm 39.2K tokens $0.0047 4.1s`, `tool write_code ✓ 0.3s`). It contains zero control-flow logic — the loop never asks the display anything. One reusable trick: a live LLM-wait spinner runs on a background thread, and `LLMWaitAwareStreamHandler` — a `logging.StreamHandler` subclass registered globally with a lock-guarded active-display slot (`agent/tui/single_run.py:21-43`) — calls `display.prepare_for_external_output()` before emitting any log record, clearing the in-place spinner line so async log output never interleaves with the live timer.

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Collaborators as keyword-only params with production defaults; `runner.py` is a thin composition root | Every lifecycle stage testable with fakes; edit/rerun/batch reuse the executor with different wiring; no DI framework | Very wide signatures (~42 params on the executor); adding a setting touches several layers |
| Staging-then-promote: agent works in a run-scoped dir with fixed filenames; only verified success promotes and deletes staging | Record library only ever holds compilable assets; failures leave a full debuggable scene | Failed staging dirs accumulate until reaped; double storage during the run |
| Never trust agent-declared success: recompile out-of-band when no checkpoint artifact exists (`agent/single_run.py:420-444`) | The model can declare done right after an edit that broke the build | Redundant compile pass; mitigated by reusing the checkpoint when present |
| Write `status='running'` before launch; distinct exit codes per failure stage (2/3/4) | Hard kills leave a detectable row; scripts branch on *which* stage failed without parsing logs | Two writes per run; stale `running` rows need a reaper |
| Provenance-first reproducibility: `provenance.json` complete enough that edit/rerun reconstruct ALL settings, cascade explicit > stored > env > default | Any asset regenerable months later with the exact original config, plus targeted overrides | Provenance schema becomes a compatibility surface; reconstruction logic duplicated across edit and rerun |
| Edit = fork to a NEW record seeded with parent code; rerun = new REVISION of the SAME record, no seed | Fork preserves the parent and gives the model working code; rerun refreshes an entry while keeping its identity | Two paths sharing ~70% of reconstruction logic; users must learn fork-vs-revision |
| Edit context is artifact-based (prepended guidance in the first user turn), never a replayed transcript | Cheap, provider-agnostic, no context blowup; preempts hallucinated memory; system prompt stays cacheable | Reasoning from the original session not reflected in code/prompt is lost |
| Lexical BM25 + rule rerank + precision gates for few-shot retrieval, exposed as a tool | Deterministic, offline, dependency-light; on-demand retrieval keeps turn-1 small; gates stop anchoring on bad matches | Many hand-tuned magic numbers per corpus; no semantic matching for synonyms |
| IDs from timestamp + content hash + slug, not UUIDs | Human-scannable, time-sortable directory listings; collision-resistant via digest | Explicit `exists()` guards still needed for user-supplied ids |
| `--dump-provider-payload` dry-run exits before any API call | Audit the exact provider request with zero spend | Preview path must share code with the real request builder or it silently lies |
| Display is a pure observer fed by callbacks; a logging handler clears the live spinner line before each emit | Loop logic stays UI-free and headless-runnable; async logs never corrupt the status line | Global mutable handler slot needs a lock; observer interface grows with every new event kind |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| `DEFAULT_MAX_TURNS` (`agent/defaults.py:3`) | 100 | Self-correcting compile loops need many tool turns |
| Per-model turn override (`agent/defaults.py:4,12-15`) | 250 for the cheap/fast model | Low per-turn cost buys more attempts |
| Failure exit codes (`agent/single_run.py:391,437,497`) | 0 ok / 1 validation / 2 agent / 3 verify / 4 persist | Callers branch on the failing stage |
| ID recipe (`agent/run_context.py:348-352`) | `run_{ts}_{sha1[:8]}`, slug ≤ 48 | Readable + sortable + collision-resistant |
| `MAX_SINGLE_RUN_SLUG_LEN` (`agent/run_context.py:28`) | 120 | Filesystem-safe; overlong slugs truncated + hash-suffixed |
| Revision format (`storage/revisions.py:13-14`) | `rev_%06d`, starts `rev_000001` | Zero-padded ids sort lexicographically; next = max on disk + 1 |
| Field repetitions (`agent/examples.py:39-46`) | slug 6 / title 5 / tags 4 / desc 3 / code 2 / prose 1 | Field weighting inside one flat BM25 index |
| Rerank bonuses (`agent/examples.py:47-70`) | exact slug 4.0 → prose 0.5; alias ×0.3; coverage²×3.0 | Metadata dominates body text; full-coverage docs win |
| Candidate pool (`agent/examples.py:706`) | `min(n_docs, max(limit*6, 12))` | Wide pool for reranking without scoring the corpus |
| Retention floors (`agent/examples.py:609-687`) | strong ≥ `max(1.0, best*0.5)`; weak ≥ `max(1.0, best*0.7)`, cap 2 | Trim the tail relative to the best hit; weak fallback is stingier |
| Index caches (`agent/examples.py:444,454`) | `lru_cache(maxsize=8)` keyed by SDK package | Parse and index once per process per corpus |
| Image detail default (`agent/tools/__init__.py:164`) | `"high"` | Reference images drive output decisions; max vision fidelity |
| Prompt preview (`agent/run_context.py:177`) | 160 chars | Truncated preview for record listings |

## Reusable pattern

```python
# Run-lifecycle orchestration: staging -> verify -> promote, with fork and rerun.
import asyncio, hashlib, shutil, time
from pathlib import Path

def build_context(prompt, store, record_id=None, revision_id=None):
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    digest = hashlib.sha1(prompt.encode()).hexdigest()[:8]
    ctx = Context(
        run_id=f"run_{ts}_{digest}",
        record_id=record_id or f"rec_{slugify(prompt)[:48]}_{ts}_{digest}",
        revision_id=revision_id or "rev_000001",
    )
    ctx.staging = store.staging_root / ctx.run_id     # fixed filenames inside:
    ctx.staging.mkdir(parents=True)                   # prompt.txt, source, checkpoint, traces/, cost.json
    return ctx

async def execute_run(user_content, settings, ctx, *, seed_metadata=None,
                      agent_cls=Agent, verify=verify_artifact, persist=write_record,
                      observer=None):
    def fail(code, msg, **counts):                    # one failure sink per run
        store.write_run(ctx.run_id, status="failed", msg=msg,
                        staging=str(ctx.staging), **counts)   # KEEP staging for debugging
        return Outcome(exit_code=code, status="failed", message=msg)

    store.write_run(ctx.run_id, status="running", settings=settings.snapshot())  # BEFORE launch
    (ctx.staging / "prompt.txt").write_text(settings.prompt)
    try:
        async with agent_cls(file=ctx.staging / "source", limits=settings,
                             observer=observer) as agent:     # observer: pure callbacks, no logic
            result = await agent.run(user_content)
            actual_model = agent.llm.model_id         # what actually ran, not what was requested
    except Exception as e:
        return fail(2, f"runtime: {e}")
    if not result.success:
        return fail(2, result.message, **result.counts)
    # Never trust the agent's success claim without an artifact:
    artifact = result.checkpoint_artifact or await asyncio.to_thread(
        verify, ctx.staging / "source")
    if artifact is None:
        return fail(3, "verification failed", **result.counts)
    try:
        record_dir = await asyncio.to_thread(
            persist, ctx, artifact,
            provenance=settings.snapshot(model=actual_model),  # FULL settings snapshot
            **(seed_metadata or {}))                  # lineage / revision_parent / revision_seed
        shutil.rmtree(ctx.staging)                    # clean ONLY on success
    except Exception as e:
        return fail(4, f"persist: {e}", **result.counts)
    store.write_run(ctx.run_id, status="success", model=actual_model)
    return Outcome(exit_code=0, status="success", record_dir=record_dir)

def resolve(explicit, stored, default):               # universal override cascade
    if explicit is not None: return explicit
    if stored is not None:   return stored
    return default

async def fork_record(parent_id, edit_prompt, **overrides):   # FORK: new record, code-seeded
    parent, prov = store.load(parent_id), store.load_provenance(parent_id)
    settings = {k: resolve(overrides.get(k), prov.get(k), DEFAULTS[k]) for k in SETTING_KEYS}
    ctx = build_context(edit_prompt, store)                   # fresh record, rev_000001
    shutil.copy2(store.active_source(parent_id), ctx.staging / "source")  # seed parent code
    guidance = (f"{FORK_GUIDANCE}\n"                          # includes: "no prior conversation
                f"Parent: {parent_id}@{parent.active_rev}\n"  #  history exists; staged code is
                f"Parent prompt: {parent.prompt}")            #  the source of truth"
    return await execute_run(prepend_guidance(guidance, edit_prompt), settings, ctx,
        seed_metadata=dict(
            lineage={"origin": parent.origin or parent_id, "parent": parent_id, "mode": "copy"},
            revision_seed={"record": parent_id, "artifact": "source"},
            inherited_inputs=hash_and_dedupe_inputs(parent)))  # sha256 content-addressed chain

async def rerun_record(record_id, **overrides):               # RERUN: same record, new revision
    rec, prov = store.load(record_id), store.load_provenance(record_id)
    settings = {k: resolve(overrides.get(k), prov.get(k), DEFAULTS[k]) for k in SETTING_KEYS}
    ctx = build_context(rec.prompt, store, record_id=record_id,
                        revision_id=store.next_revision(record_id))
    return await execute_run(rec.prompt, settings, ctx,        # NO code seed: regenerate
        seed_metadata=dict(revision_parent=rec.active_rev, revision_seed=None))

# Spinner-safe logging: clear the live status line before any async log record.
import logging, threading
_ACTIVE_DISPLAY, _LOCK = None, threading.Lock()

class LiveLineAwareHandler(logging.StreamHandler):
    def emit(self, record):
        with _LOCK:
            display = _ACTIVE_DISPLAY
        if display is not None:
            display.clear_live_line()                 # erase in-place spinner/timer first
        super().emit(record)
```

## Pitfalls

- Persist the ACTUAL model id the provider reports after the run, not the requested one (`agent/single_run.py:388`) — providers alias model ids, and provenance written from the request silently breaks future reruns.
- Do not let the agent self-certify success: re-verify out-of-band when no checkpoint artifact exists (`agent/single_run.py:420-444`).
- Write the `running` status row BEFORE launching the agent, or a hard kill leaves no trace the run started (`agent/single_run.py:332-359`); then plan a reaper for stale `running` rows.
- Delete staging only in the success branch and record its path in the failure row (`agent/single_run.py:493-494`) — deleting on all paths destroys the only debug evidence.
- Guard record-id collisions TWICE in fork flows: once for user-supplied ids, once for generated ones (`agent/edit.py:291-298`, `328-335`).
- In fork/edit runs, explicitly tell the model no prior conversation history exists and the staged code is the source of truth (`agent/edit.py:43-46`); otherwise models hallucinate memory of the original session.
- Stored provenance can name a system prompt that is not a real prompt (e.g. records created by an external agent); both fork and rerun must detect this and substitute a valid one (`agent/edit.py:50-58`, `agent/rerun.py:44-53`) — and the guard constant duplicated across both files is a drift hazard.
- Inject derived-run context into the first USER turn, never the system prompt — keeps the system prompt identical and cacheable across run kinds (`agent/tools/__init__.py:119-136`).
- Raw BM25 over a small corpus returns something for almost any query; without precision gates and a `weakly_relevant` label the model anchors on junk. Prefer `[]` over noise, and say so in the tool description.
- Tool descriptions are prompt engineering: state what query shapes work, what the tool does NOT search, and how to treat weak results (`agent/tools/find_examples.py:77-89`).
- Dedupe inherited input references by `(record_id, revision_id, path)` and attach sha256 digests when chaining inputs across forks (`agent/edit.py:61-129`), or multi-generation lineages silently drift.
- Build the dry-run payload preview through the SAME code path as the real request (`agent/runner.py:207-213`) or it lies.
- Rerun must NOT seed the parent's code (`agent/rerun.py:212`) — a rerun that starts from existing code is an edit in disguise and corrupts revision semantics.
- Route every blocking call in the async lifecycle through `asyncio.to_thread`, and bound CPU-heavy verification with a shared concurrency slot for batch mode.
- A live spinner plus async logging corrupts the terminal; subclass the log handler to clear the live line before every emit (`agent/tui/single_run.py:25-32`) instead of hoping outputs don't interleave.

## Checklist

- [ ] Composition root wires concrete implementations into keyword-only DI seams; lifecycle functions never import their production collaborators directly
- [ ] Setting resolution follows one cascade everywhere: explicit flag > env > stored provenance > inference > default
- [ ] Pre-flight gates before any API spend: credential validation with exit 1, and a payload dry-run flag sharing the real request builder
- [ ] One frozen context object mints run id, record id, revision id, staging paths, and promotion paths up front; IDs are timestamp + content hash + slug
- [ ] Staging dir with fixed filenames; promote to the record store only on verified success; keep staging on failure and record its path
- [ ] `status='running'` row written before agent launch; distinct process exit code per failure stage (agent / verify / persist)
- [ ] Runner independently verifies the final artifact when the agent left no fresh checkpoint
- [ ] Actual post-run model id (not the requested id) written into provenance
- [ ] Provenance snapshot complete enough that fork and rerun reconstruct every setting without the original CLI invocation
- [ ] Fork = new record + copied parent code + lineage + `revision_seed`; rerun = same record + `next_revision_id` + no seed
- [ ] Derived-run context injected as prepended guidance in the first user turn, including "no prior history exists"
- [ ] Inherited inputs content-addressed (sha256) and deduped across generations
- [ ] Few-shot retrieval (if any) exposed as an on-demand tool with precision gates, a weak-match label, and a coaching schema description
- [ ] Progress display is a pure observer fed by harness callbacks; loop runs headless without it
- [ ] Live status lines and async logging reconciled via a line-clearing log handler
- [ ] All blocking I/O in the async lifecycle goes through `asyncio.to_thread`
