# 08. State, Traces & Persistence

**Maps to:** State/Context · Memory · Cost · Guardrails · Evaluator/Verifier · **Distilled from:** Articraft `agent/run_context.py`, `agent/single_run.py`, `agent/record_persistence.py`, `agent/traces.py`, `agent/harness_codec.py`, `storage/{layout,revisions,trajectories,materialize,runs}.py`

## Why this module exists

An agent that writes files while it thinks will crash mid-run, fail verification after spending the whole LLM budget, or get rerun over its own previous output — and each of those must leave the durable library in a known-good state. This module solves that with one rule: the agent works only in a per-run staging directory, and artifacts reach the permanent store only after an independent verifier passes, via a single commit function. It also answers "what exactly produced this artifact?" (full provenance: model settings, prompt hash, git commit, lockfile hash) and "what did the agent actually do?" (a crash-safe streaming trace of every message and tool call), which is what makes failures debuggable and successes reproducible and minable for training data. Without it, half-finished outputs pollute the library, crashed runs are indistinguishable from running ones, and traces exist only in memory.

## How Articraft implements it

### Frozen run context and sortable IDs

`_build_single_run_context` mints all identifiers and precomputes every path the run will ever touch into a frozen dataclass before anything executes (`agent/run_context.py:337-381`, dataclass at `agent/run_context.py:31-53`). IDs are `run_{UTC %Y%m%d_%H%M%S_%f}_{sha1(prompt)[:8]}` and `rec_{slug(prompt)[:48]}_{token}_{digest}` (`agent/run_context.py:348-352`) — lexicographically time-sorted, human-scannable, collision-resistant. Slugs are capped at 120 chars with a 10-char sha1 suffix on overflow (`agent/run_context.py:110-133`). Revision IDs are a separate fixed-width series `rev_000001…` validated by `^rev_[0-9]{6}$` at every boundary (`storage/revisions.py:13-23`), with `next_revision_id` scanning existing dirs for max+1 (`storage/revisions.py:31-41`).

### Stage-then-commit lifecycle

Staging lives under the run, not the record: `data/cache/runs/{run_id}/staging/{record_id}/` (`agent/run_context.py:355-356`). The agent writes code, checkpoint artifacts, traces, and the cost ledger only there. After both agent success and an independent final compile pass, `write_success_record` copies staging into the permanent dirs and only then deletes staging (`agent/single_run.py:492-494`). On any failure staging is retained and its repo-relative path is written into the failure row for debugging (`agent/single_run.py:279`).

### Three-tier permanent layout

Permanent state is split by mutability and regenerability (`agent/record_persistence.py:546-848`): (1) `data/records/{id}/record.json` — the mutable head: display metadata, tags/rating, lineage, and the `active_revision_id` pointer (`agent/record_persistence.py:775-842`); (2) `revisions/rev_NNNNNN/` — immutable snapshots of prompt, source code, provenance, cost, inputs, traces, plus `revision.json` with content hashes and parent/seed lineage (`storage/revisions.py:181-212`, written at `agent/record_persistence.py:741-762`); (3) `data/cache/record_materialization/{id}/` — derived, regenerable outputs stamped with a fingerprint `sha256(code_sha|artifact_sha|sdk_fp|materializer_version)` so staleness is detectable and the whole cache is safe to delete (`storage/materialize.py:21-71`). Readers resolve artifacts through one function, `active_artifact_path`, which carries schema-version fallback logic (`storage/revisions.py:75-106`).

### Streaming JSONL trace, compressed at rest

`TraceWriter` appends one JSON object per event — `{"ts": time.time(), "type": ..., ...payload}` — and flushes after every write, so a crash loses at most one line (`agent/traces.py:31-38`). The harness routes every assistant message, tool result, injected reminder, compile-failure feedback, and provider diagnostic through it (`agent/harness.py:420,474,548,717,1172-1528`). At commit, `canonicalize_record_trace_dir` compresses `trajectory.jsonl` to `.jsonl.zst` at zstd level 19, deletes the plain file, and strips legacy per-trace prompt copies (`storage/trajectories.py:109-131`); decompression on demand goes to a cache with mtime-based skip (`storage/trajectories.py:134-176`).

### Provenance capture

Every commit writes a `provenance.json` with four sections — generation (model/settings/limits), prompting (system-prompt name + sha256), sdk (package/version/fingerprint), environment (python version, platform, `git rev-parse HEAD` or None, lockfile sha256) — plus a run summary of turn/tool/attempt counts (`agent/record_persistence.py:678-714`, env capture at `agent/run_context.py:191-218`). The multi-KB system prompt is deduplicated into a content-addressed store `data/system_prompts/{sha256}.txt` with a collision check on content, and revisions store only the hash (`storage/trajectories.py:52-66`).

### Typed failure paths and crash markers

`run.json` is written with `status="running"` before the agent starts (`agent/single_run.py:333-359`) — a stale "running" is the crash marker. Three failure exits route through `_persist_failure` (`agent/single_run.py:264-330`): exit 2 = agent exception or agent-reported failure (`:390-403`), 3 = final verification failure (`:437-443`), 4 = persist-step failure (`:495-503`). Failure persists metadata only — status flip plus an appended `results.jsonl` row with message and staging path; no record dir is ever created. `results.jsonl` is append-only with last-write-wins reads per record_id (`storage/runs.py`), so retries just append.

### Referenced-asset pruning at commit

Instead of copying the whole staging asset tree, the final artifact XML is parsed and only the files it actually references are copied; refs that are absolute or contain `..` are dropped as a path-traversal guard (`agent/record_persistence.py:174-197`), and a referenced-but-missing asset raises, failing the commit (`agent/record_persistence.py:212-231`). The committed derived cache is therefore exactly self-consistent with the committed artifact — orphans from earlier compile attempts never leak into the library.

### Provider-neutral message codec

`MessageCodec` normalizes provider-specific message dicts into one canonical `{role, thought_summary?, content?, tool_calls?, extra_content?, usage?}` schema before they enter conversation state or the trace (`agent/harness_codec.py:40-58`), handling three tool-call encodings (`:60-87`). Because traces store the canonical form, `trajectory.jsonl` has one schema regardless of provider — critical for later training-data extraction. Parallel tool execution is gated to one provider and a frozenset allowlist of read-only tools (`agent/harness_codec.py:8,89-94`).

### Rerun semantics and tolerant readback

`write_success_record` accepts the existing record and preserves human-owned fields across an overwrite — created_at, rating, author, display title — refreshing only `updated_at` (`agent/record_persistence.py:775-841`); draft creation instead refuses to clobber an existing id (`agent/record_persistence.py:372-373`). Cost-ledger readback is fully defensive: any IO/decode/shape error returns `(None, None)` instead of raising, and only allowlisted int token fields are extracted (`agent/run_context.py:73-107`).

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| All in-flight state in per-run staging; commit to library only after verified success, then delete staging; keep it on failure | Library never contains half-finished entries; failed runs stay fully debuggable on disk | Commit is a multi-file copy, not atomic — mitigated only by a post-commit existence check; failure staging grows until swept |
| Three-way split: mutable head + immutable revisions + regenerable derived cache keyed by content fingerprint | Free edit history and lineage; derived outputs rebuildable, so the cache is deletable wholesale and the source of truth stays small | Every reader pays the `active_revision_id` indirection; disk grows per revision; staleness needs fingerprint checks |
| Flush-per-event JSONL during the run; zstd-19 + delete plain file at commit; decompress-on-demand cache | Crash-safe and tail-able live; ~10x smaller at rest; repeated reads cheap | Not greppable in place; a syscall per event; CPU spent at commit |
| Content-address the system prompt into a shared store; provenance stores name + sha256 only | Identical multi-KB prompt across thousands of records deduped; sha pins the exact version | Deleting a prompt file orphans every record referencing its sha |
| Failure persists metadata only (status flip + append-only results row); `status="running"` written before the agent starts | Browsable library stays success-only; stuck "running" detects crashes; appends never corrupt prior rows | Failures are second-class (no provenance, no compressed trace); results log needs explicit compaction |
| Parse the final artifact and copy only referenced assets, rejecting absolute/`..` paths, raising on missing refs | Committed output is minimal and exactly consistent; silent asset drift becomes a hard failure | An unparseable artifact or missing asset fails the run at the last step, after all LLM spend |
| Precompute every path into one frozen context dataclass; pass the commit as one frozen request object | One constructor owns path derivation — no path-string drift across runner/harness/persistence; commit signature stays evolvable and injectable for tests | New artifacts touch context + layout + commit together; request duplicates some context fields |
| Normalize all provider messages through one codec before state/trace | One trace schema regardless of provider | Codec must be extended per new provider encoding |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| `MAX_SINGLE_RUN_SLUG_LEN` | 120 (48 inside record_id) | Keeps prompt-derived paths filesystem-safe; overflow appends a 10-char sha1 (`agent/run_context.py:28,124-133,349`) |
| ID digest lengths | `sha1[:8]` in ids, `[:10]` for slug overflow | Disambiguates identical timestamps without bloating ids (`agent/run_context.py:129,350`) |
| `_ZSTD_LEVEL` | 19 | Write-once read-rarely traces justify max-side compression CPU (`storage/trajectories.py:23`) |
| Revision id format | `rev_%06d`, regex `^rev_[0-9]{6}$` | Fixed-width ids sort lexicographically; strict validation at every boundary (`storage/revisions.py:13-14`) |
| Schema versions | record=3, provenance=2, revision=1, run=1 | Per-document versioning lets readers apply fallback logic for old layouts (`agent/record_persistence.py:679,776`, `storage/revisions.py:198`, `agent/single_run.py:336`) |
| Failure exit codes | 0/2/3/4 = success / agent / verify / persist | Callers and batch orchestration see which stage failed (`agent/single_run.py:391-403,437-443,495-503`) |
| `materializer_version` | "v1" | Folded into the derived-cache fingerprint so bumping it invalidates every cached output (`storage/materialize.py:26`) |
| Display bounds | preview 160 chars, title 120 chars | Denormalized into the head doc so list UIs never read full prompts (`agent/run_context.py:177-188`) |
| Hash chunk size | 1 MiB | Constant-memory hashing of large artifacts (`storage/revisions.py:162`) |
| `PARALLEL_SAFE_TOOL_NAMES` | frozenset of 3 read-only tools | Mutation stays strictly serial; only pure reads may batch (`agent/harness_codec.py:8`) |

## Reusable pattern

```python
"""Stage-then-commit persistence for an LLM agent. stdlib only."""
import hashlib, json, lzma, re, shutil, subprocess, sys, time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("data")
REVISION_RE = re.compile(r"^rev_[0-9]{6}$")

class Layout:
    """One class owns EVERY path. Three durability tiers."""
    # Tier A: permanent source of truth (small, human-meaningful)
    @staticmethod
    def record_dir(rid): return DATA / "records" / rid            # record.json = mutable head
    @staticmethod
    def revision_dir(rid, rev): return DATA / "records" / rid / "revisions" / rev
    # Tier B: regenerable derived-output cache (safe to delete wholesale)
    @staticmethod
    def derived_dir(rid): return DATA / "cache" / "derived" / rid
    # Tier C: per-run scratch — committed on success, kept for debugging on failure
    @staticmethod
    def staging_dir(run_id, rid): return DATA / "cache" / "runs" / run_id / "staging" / rid
    @staticmethod
    def run_meta(run_id): return DATA / "cache" / "runs" / run_id / "run.json"
    @staticmethod
    def results_log(run_id): return DATA / "cache" / "runs" / run_id / "results.jsonl"
    @staticmethod
    def shared_prompt(sha): return DATA / "system_prompts" / f"{sha}.txt"  # content-addressed

def _slug(text, cap=48):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:cap] or "task"

def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):     # constant memory
            h.update(chunk)
    return h.hexdigest()

@dataclass(frozen=True)
class RunContext:
    """Mint ids + precompute every path BEFORE anything executes."""
    run_id: str; record_id: str; revision_id: str
    staging: Path; revision: Path; derived: Path

def build_context(task_text):
    token = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    digest = hashlib.sha1(task_text.encode()).hexdigest()[:8]
    run_id = f"run_{token}_{digest}"                             # time-sorted, unique
    record_id = f"rec_{_slug(task_text)}_{token}_{digest}"       # scannable + unique
    rev = "rev_000001"                                           # fixed-width, regex-validated
    staging = Layout.staging_dir(run_id, record_id)
    (staging / "traces").mkdir(parents=True, exist_ok=True)
    return RunContext(run_id, record_id, rev, staging,
                      Layout.revision_dir(record_id, rev), Layout.derived_dir(record_id))

class TraceWriter:
    """Crash-safe streaming trace: one JSON object per line, flush per event."""
    def __init__(self, trace_dir):
        self._f = open(trace_dir / "trajectory.jsonl", "x")      # "x": never truncate a prior trace
    def event(self, event_type, **payload):
        self._f.write(json.dumps({"ts": time.time(), "type": event_type, **payload}) + "\n")
        self._f.flush()                                          # lose at most the current line
    def close(self): self._f.close()

def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.replace(path)                                            # atomic rename: no torn JSON

def append_jsonl(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")   # append-only; readers use last row per record_id

EXIT_AGENT, EXIT_VERIFY, EXIT_PERSIST = 2, 3, 4                  # typed stage failures

def execute(ctx, task_text, agent, verifier):
    write_json(Layout.run_meta(ctx.run_id),
               {"schema_version": 1, "status": "running", "record_id": ctx.record_id})  # crash marker
    trace = TraceWriter(ctx.staging / "traces")
    try:
        result = agent.run(task_text, workdir=ctx.staging, trace=trace)
    except Exception as exc:
        return persist_failure(ctx, EXIT_AGENT, repr(exc))
    finally:
        trace.close()
    if not result.success:
        return persist_failure(ctx, EXIT_AGENT, result.message)
    try:
        artifact = verifier(ctx.staging)          # independent final check, never agent-reported
    except Exception as exc:
        return persist_failure(ctx, EXIT_VERIFY, repr(exc))
    try:
        commit(ctx, task_text, result, artifact)
        shutil.rmtree(ctx.staging)                # delete staging ONLY after verified commit
    except Exception as exc:
        return persist_failure(ctx, EXIT_PERSIST, repr(exc))
    write_json(Layout.run_meta(ctx.run_id), {"schema_version": 1, "status": "success"})
    append_jsonl(Layout.results_log(ctx.run_id),
                 {"record_id": ctx.record_id, "revision_id": ctx.revision_id, "status": "success"})
    return 0

def persist_failure(ctx, code, message):
    """Metadata only: NO record is created; staging is KEPT and its path logged."""
    write_json(Layout.run_meta(ctx.run_id), {"schema_version": 1, "status": "failed"})
    append_jsonl(Layout.results_log(ctx.run_id),
                 {"record_id": ctx.record_id, "status": "failed",
                  "message": message, "staging_dir": str(ctx.staging)})
    return code

def commit(ctx, task_text, result, artifact):
    rev = ctx.revision
    rev.mkdir(parents=True, exist_ok=True)
    (rev / "prompt.txt").write_text(task_text)
    (rev / "source_code.py").write_text(result.code)
    copy_referenced_assets_only(artifact, ctx.staging / "assets", ctx.derived / "assets")
    if (ctx.staging / "cost.json").exists():
        shutil.copy2(ctx.staging / "cost.json", rev / "cost.json")
    shutil.copytree(ctx.staging / "traces", rev / "traces", dirs_exist_ok=True)
    canonicalize_trace(rev / "traces" / "trajectory.jsonl")
    prompt_sha = dedupe_system_prompt(result.system_prompt)
    write_json(rev / "provenance.json", {
        "schema_version": 1,
        "generation": result.model_settings,                    # provider, model, limits
        "prompting": {"system_prompt_sha256": prompt_sha},
        "environment": {"python": sys.version.split()[0],
                        "git_commit": _git_commit(),            # best-effort: None if no repo
                        "lockfile_sha256": _sha256_file(Path("uv.lock"))
                            if Path("uv.lock").exists() else None},
        "run_summary": {"turns": result.turns, "tool_calls": result.tool_calls,
                        "final_status": "success"}})
    write_json(rev / "revision.json", {"schema_version": 1, "source_run_id": ctx.run_id,
                                       "code_sha256": _sha256_file(rev / "source_code.py"),
                                       "parent_revision_id": None})
    head = Layout.record_dir(ctx.record_id) / "record.json"
    existing = json.loads(head.read_text()) if head.exists() else {}
    write_json(head, {"schema_version": 1, "record_id": ctx.record_id,
                      "active_revision_id": ctx.revision_id,     # mutable head pointer
                      "created_at": existing.get("created_at", time.time()),
                      "rating": existing.get("rating"),          # human fields survive reruns
                      "updated_at": time.time()})
    for required in ("source_code.py", "provenance.json"):       # post-commit invariant
        if not (rev / required).exists():
            raise FileNotFoundError(rev / required)

def canonicalize_trace(plain):
    compressed = plain.with_suffix(plain.suffix + ".xz")         # swap for zstd if available
    with open(plain, "rb") as src, lzma.open(compressed, "wb") as dst:
        shutil.copyfileobj(src, dst)
    plain.unlink()

def dedupe_system_prompt(text):
    sha = hashlib.sha256(text.encode()).hexdigest()
    path = Layout.shared_prompt(sha)
    if path.exists():
        if path.read_text() != text:
            raise RuntimeError(f"sha256 collision at {path}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return sha

def copy_referenced_assets_only(artifact, src_root, dst_root):
    """Parse the FINAL artifact; copy only what it references. Strict by design."""
    refs = [r for r in artifact.referenced_files()
            if not Path(r).is_absolute() and ".." not in Path(r).parts]  # traversal guard
    if dst_root.exists():
        shutil.rmtree(dst_root)
    for ref in refs:
        src = src_root / ref
        if not src.exists():
            raise FileNotFoundError(src)             # missing ref = hard persist failure
        dst = dst_root / ref
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

def _git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return None
```

## Pitfalls

- Articraft's `write_json`/`write_text` are plain writes — no temp-file+rename, no fsync (`storage/repo.py:31-37`) — so power loss can leave torn JSON. Use atomic rename (as in the skeleton) unless you deliberately accept the staging-order + existence-check mitigation.
- `_replace_tree_from_source` deletes the destination before checking the source exists (`agent/record_persistence.py:157-162`); a rerun whose staging lacks a subtree silently erases the previous revision's copy. Validate the source before any destructive delete.
- `TraceWriter` opens the trace in `"w"` mode (`agent/traces.py:25`) — a second writer on the same dir truncates an existing trace, and its `close()` swallows all exceptions. Open with `"x"` and let close failures surface.
- The strict referenced-asset check fails the whole run at persist time, after all LLM spend (`agent/record_persistence.py:197,222,228`). Correct for library integrity — but run a cheap artifact-parse sanity check earlier in the loop so the expensive failure mode is rare.
- Overwrite semantics must be an explicit decision per field: Articraft's success path intentionally overwrites but inherits rating/created_at/author (`agent/record_persistence.py:775-841`), while draft creation refuses to clobber (`:372-373`). If you skip this, reruns silently destroy human curation.
- Retained failure staging and append-only `results.jsonl` grow unboundedly; compaction exists (`storage/runs.py`) but must be invoked. Budget a GC/sweep path from day one.
- Provenance capture is best-effort — git commit, lockfile sha, author all record `None` on any exception (`agent/run_context.py:191-214`). Reproducibility silently degrades outside a git checkout; decide whether that should warn.
- Tolerant readback hides corruption: malformed cost ledgers become `(None, None)` with no signal (`agent/run_context.py:73-107`). Log a warning even when you return the tolerant default.
- Schema migrations never fully retire — `active_artifact_path` still carries pre-v3 fallback logic (`storage/revisions.py:104-106`). Version every on-disk document from day one and centralize resolution in one function.
- A stale `status="running"` in run.json is the only marker for hard-killed processes; every run reader must treat it as failed after a timeout, not as in-progress.

## Checklist

- [ ] One `Layout` class owns every path; nothing else concatenates path strings
- [ ] Three tiers separated: permanent source of truth / regenerable derived cache / per-run staging
- [ ] Frozen run context mints IDs and precomputes all paths before the agent starts
- [ ] IDs are time-sorted, human-scannable, and digest-suffixed for uniqueness
- [ ] Agent writes only to staging; permanent store touched only by one commit function
- [ ] Commit runs only after an independent verifier passes (never trust agent self-report)
- [ ] Staging deleted only after successful commit; retained and logged on failure
- [ ] `run.json` status transitions running → success/failed; stale "running" treated as crashed
- [ ] Typed exit codes distinguish agent / verify / persist failures
- [ ] Trace is flush-per-event JSONL during the run, compressed at rest after commit
- [ ] All provider messages normalized to one canonical schema before state and trace
- [ ] Provenance captures model settings, prompt sha256, git commit, lockfile hash, run counts
- [ ] Large shared blobs (system prompt) content-addressed with a collision check
- [ ] Derived outputs fingerprinted (code sha + toolchain version) for staleness detection
- [ ] Commit copies only assets the final artifact references; rejects absolute/`..` paths
- [ ] Post-commit invariant check verifies canonical artifacts exist
- [ ] Rerun/overwrite field inheritance decided explicitly; human-owned fields preserved
- [ ] Small JSON docs written via temp-file + atomic rename; every document carries `schema_version`
- [ ] GC path planned for failure staging and append-only result logs
