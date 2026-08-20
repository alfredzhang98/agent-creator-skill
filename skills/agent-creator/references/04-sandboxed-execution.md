# 04. Sandboxed Execution & Resource Limits

**Maps to:** Tools · Guardrails · Evaluator/Verifier · Executor · Cost · **Distilled from:** Articraft `agent/tools/probe_model/{tool.py,runner.py,helpers.py}`, `agent/mp_utils.py`, `agent/runtime_limits.py`, `agent/open_file_limits.py`, `agent/compiler.py`

## Why this module exists

Agents that build code artifacts eventually need to *run* code — either the artifact itself (compile/verify) or LLM-authored inspection snippets against it ("measure the gap between these two parts"). Both are untrusted workloads inside a trusted process: they can hang forever, segfault in native libraries, leak unbounded memory, or corrupt interpreter state. In-process `exec()` gives you none of the guarantees you need; a separate OS process is the only boundary that is reliably killable on timeout, converts native crashes into parseable data, and returns all memory on exit. This module covers the full stack: the subprocess protocol (JSON-in/JSON-out with a typed error taxonomy the LLM can self-correct from), the resource governance around it (shared concurrency semaphore, wall-clock timeouts, FD-budget sizing), and the curated helper namespace that makes the sandboxed snippet productive instead of raw.

## How Articraft implements it

### Parent-side launch: fresh interpreter, hard timeout, kill-and-reap

`execute()` serializes a request dict `{file_path, sdk_package, code}` to JSON, then spawns a completely fresh interpreter with `asyncio.create_subprocess_exec(sys.executable, "-m", "agent.tools.probe_model.runner")`, cwd pinned to the repo root (`agent/tools/probe_model/tool.py:47-89`). The request goes to the child's stdin; the reply is read from stdout via `process.communicate()` wrapped in `asyncio.wait_for(timeout_ms/1000)`. On `asyncio.TimeoutError` the parent calls `process.kill()` and then awaits `communicate()` **again** to reap the zombie (`tool.py:78-79`), returning an in-band `{ok: false, error: {type: "timeout"}}` payload rather than raising — the LLM sees the timeout as data and can narrow its probe. Timeout enforcement lives entirely in the parent because LLM-authored child code cannot be trusted to self-limit. The launch is gated by a shared local-work semaphore (`tool.py:62`, see below), and the timeout is validated to be `>= 100ms` up front (`tool.py:50-51`).

### Defensive parsing ladder: child output is untrusted

The parent never assumes the child produced valid output (`tool.py:90-152`). It branches, in order: nonzero returncode → `runner_process_error` with raw stdout/stderr attached (this is how a segfault in a native CAD lib surfaces as data); empty stdout → `invalid_runner_output`; `json.loads` failure → `invalid_runner_output`; payload not a dict → `invalid_runner_output`; `payload["ok"]` not a bool → malformed payload. Note `returncode not in (0, None)` — `None` is treated as the success path (`tool.py:92`). Finally, unless the LLM passed `include_stdout=true`, the keys `stdout`/`stderr`/`runner_stdout`/`runner_stderr` are popped before returning (`tool.py:147-151`): debug prints are captured but cost tokens only on request.

### Runner protocol: exit 0 always, JSON carries the signal

The child's `main()` reads the whole request from stdin; even a malformed request produces an `{ok: false, error.type: "invalid_request"}` payload and **exit code 0** (`agent/tools/probe_model/runner.py:92-98`). The exit code never carries error signal — only genuine crashes hit the parent's returncode branch. The runner re-executes the model script fresh via `runpy.run_path` under a module lock with chdir to the script dir (`agent/compiler.py:372-391`), requires an `object_model` global, builds the helper session, and execs the snippet with a synthetic filename `file_path.with_suffix(".probe.py")` so tracebacks are attributable (`runner.py:132`). A `stage` variable flips from `"load"` to `"exec"` right before the snippet runs, so exceptions are blamed on the correct party: model file → `load_failure`, snippet → `snippet_exception` (`runner.py:145-188`).

### emit()-exactly-once result channel with serializability pre-check

A closure-based `emit(value)` raises on a second call, and `emit_count == 0` after exec also raises (`runner.py:103-108,135-136`). The emitted value is normalized by `_jsonable` (recursing dicts/lists/tuples, special-casing domain pose objects into plain dicts, `runner.py:25-37`), then `json.dumps(emitted_value)` runs as a **pure pre-check** before payload assembly (`runner.py:138`) so a `TypeError` maps to the dedicated `non_serializable_result` error instead of corrupting the single-JSON-on-stdout protocol mid-write. `print()` output goes to separate StringIO buffers — the machine-readable channel and the debug channel never mix.

### Typed error taxonomy the LLM can act on

Every failure classifies into one of: `invalid_request`, `load_failure` (with traceback), `lookup_failure` (unknown part/joint name — the message names the bad key), `emit_contract`, `non_serializable_result`, `snippet_exception` (with traceback), plus parent-side `timeout`, `runner_process_error`, `invalid_runner_output` (`runner.py:145-188`, `tool.py:90-152`). Because `ProbeLookupError` is raised with the exact unknown name (`agent/tools/probe_model/helpers.py:165-181`), the LLM gets "Unknown part: X" instead of a raw KeyError traceback — precise, self-correctable feedback that keeps the loop alive instead of aborting it.

### Harness-bound target: the LLM never chooses the path

The tool schema exposes no path parameter. `ProbeModelInvocation` extends `BoundFileToolInvocation` (`agent/tools/base.py:118-126`); the harness injects the session's current model file via `bind_file_path()` after building any invocation (`agent/harness.py:984-986`). `build()` even pops a legacy `file_path` key from LLM params before validation (`tool.py:192-203`), so the model cannot probe arbitrary files — the tool is bound to the artifact under construction.

### Curated helper namespace instead of raw SDK access

`ProbeSession` builds name→object indexes in `__init__` (`helpers.py:87-120`) and `build_namespace()` injects ~35 callables (`helpers.py:893-929`): lookups, exact-geometry measurements, pairwise relation reports tagged with `metric_kind` and echoing resolved target refs (`helpers.py:302-355`), and O(n²) review scans that sort worst-first and truncate to `limit=10` (`helpers.py:593-616`). A `catalog()` helper returns the grouped listing so the snippet can self-discover the API. The helpers may call private `ctx._*` internals — the tool description explicitly disclaims them as non-public. Expensive projection intervals are cached keyed by `(id(element), axis)` with strong references held so GC cannot recycle ids, and the whole cache invalidates when the articulation pose changes (`helpers.py:973-1026`).

### Killable compile worker: spawn-first multiprocessing

The sibling heavy workload — URDF compilation — uses `mp.Process` + one-way `Pipe`: the parent polls with `URDF_COMPILE_TIMEOUT_SECONDS` (default 300s, env-tunable, 0 disables) and on expiry terminates the worker, joins with 2.0s grace, and raises a TimeoutError that names the env knobs to tune (`agent/compiler.py:618-700`). Start method is spawn-first, then forkserver, with an `ARTICRAFT_MP_START_METHOD` env override (`agent/mp_utils.py:23-48`): fork from a long-lived threaded async runner risks deadlocks and drags heavy native state into children. Results cross the pipe as a plain dict, so nothing unpicklable has to survive process death.

### Shared local-work semaphore and FD-budget sizing

`BatchRuntimeLimits` holds one optional `asyncio.Semaphore`; `local_work_slot(limits)` yields immediately when limits is `None` (single-run mode = unlimited) and otherwise acquires (`agent/runtime_limits.py:9-28`). The **same** limits object is threaded into the harness, all compile paths, and every probe tool instance (`agent/harness.py:204`, `agent/tools/__init__.py:90-107`), so total concurrent heavy subprocesses across N parallel rollouts is capped by a single number. For sizing, `open_file_worker_cap()` reads `RLIMIT_NOFILE` and counts open FDs via `/dev/fd` (falling back to `/proc/self/fd`), computes `max(1, (soft_limit - open_files - reserve) // per_worker_budget)`, clamps to 1 when usable ≤ 0, and returns `None` when introspection is unavailable so callers fall back to defaults (`agent/open_file_limits.py:21-69`) — each worker holds pipes, data files, and log handles, and oversizing surfaces as confusing EMFILE errors far from the cause.

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Fresh interpreter subprocess per probe, stdin-JSON / stdout-JSON | Only a process is reliably killable, converts segfaults to parseable errors, returns all memory on exit; re-executing the source means probes always see the current on-disk artifact, never stale state | Per-call interpreter start + full artifact re-execution; mitigated by generous timeout and semaphore gating, not caching |
| All failures in-band as `{ok:false, error:{type,...}}`, runner always exits 0 | The consumer is an LLM: typed errors with precise messages enable self-correction; a raised tool exception aborts the loop; exit code stays reserved for genuine crashes | Parent needs a five-step defensive parsing ladder; payload contract must be prompted |
| `emit()`-exactly-once, separate from print/stdout, with `json.dumps` pre-check | Deterministic machine-parseable result — no guessing which printed line is the answer; late TypeError becomes a dedicated fixable error type | New contract the model can violate (emit twice/never) — one more error class, stated in tool + param descriptions |
| Curated pre-injected helper namespace, not raw SDK imports | Stable documented tool-local API with `catalog()` self-discovery; implementations free to use private internals; lookup errors name the bad key | Helper layer couples to private internals and must track refactors |
| No path param; harness binds the target file, legacy param popped pre-validation | Tool is meant to inspect the artifact under construction; model-chosen paths invite probing arbitrary files | Cannot compare two files; multi-file flows need explicit rebinding |
| Strip stdout/stderr from payload by default (`include_stdout=false`) | `print()` allowed for snippet debugging without flooding LLM context on every call — direct token-cost control | Misbehaving snippet may need a second call with the flag to see its own prints |
| Spawn-first mp start method with env override; Pipe + poll-timeout + terminate for compiles | Fork-with-threads deadlocks; spawn gives clean short-lived workers whose native memory is fully reclaimed; plain-dict pipe payloads survive worker death | Spawn pays import cost per worker; env override exists for platform tuning |
| One shared semaphore for ALL heavy local work (probe + compile), FD-based sizing | Single cap protects the host regardless of which tool triggers work; `None` = zero overhead in single interactive runs | No per-tool priority — a slow compile can delay another rollout's cheap probe |
| Safety-by-contract for snippet behavior (prompt forbids writes/subprocess/network; no import blocking) | A real Python sandbox over a native CAD stack is impractical; process isolation + parent timeout + read-only target bound the blast radius | A noncompliant snippet genuinely can write files or hit the network; hard guarantees require OS-level sandboxing around the runner |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| `DEFAULT_PROBE_TIMEOUT_MS` | 600_000 (10 min) | Each call re-executes the full artifact build; generous default, per-call overridable (`tool.py:20`) |
| `timeout_ms` minimum | 100 ms | Rejects timeouts that would kill the child before startup (`tool.py:50-51`) |
| `include_stdout` default | `false` | Token economy: debug output captured but returned only on request (`tool.py:26,147-151`) |
| `URDF_COMPILE_TIMEOUT_SECONDS` | 300 s (env; 0 disables) | Hard cap on the compile worker; timeout message names tuning knobs (`compiler.py:632`) |
| Worker join grace | 2.0 s | Bounded wait after `terminate()` before giving up on a clean exit (`compiler.py`) |
| Start-method preference | `("spawn", "forkserver")` | Isolates heavy native state; avoids fork-with-threads hazards; env-overridable (`mp_utils.py:8,44-47`) |
| Review scan limits | risks/floating limit=10, neighbors=5, samples=32 | Caps token cost of O(n²) scans; sorted worst-first before truncation (`helpers.py:555-621`) |
| FD worker cap formula | `max(1, (soft - open - reserve) // per_worker)` | Sizes batch concurrency to RLIMIT_NOFILE; clamp to 1 keeps batches progressing (`open_file_limits.py:57-61`) |
| Contact tolerance | 1e-6 | Distance at/below which exact geometries count as touching (`helpers.py:480`) |
| Domain heuristic thresholds | mount gap ≤ 0.02; floating > 0.05; symmetry ≤ 0.05; outlier > 2σ | Model-unit heuristics for composite reports — retune for your domain (`helpers.py:517,636,762,674-677`) |

## Reusable pattern

```python
# Subprocess-isolated "probe" tool: let the LLM run inspection code against
# its own generated artifact, safely, with structured typed feedback.
import asyncio, json, sys

DEFAULT_TIMEOUT_MS = 600_000

# ---------- parent (inside the agent process) ----------
class ProbeTool:
    """Schema exposed to the LLM: {code, timeout_ms?, include_stdout?}.
    NO target/path parameter — the harness binds the current artifact."""

    def bind_target(self, path):          # called by the harness, never the LLM
        self.target = path

    async def execute(self, params, work_semaphore=None):
        if params.get("timeout_ms", DEFAULT_TIMEOUT_MS) < 100:
            return {"ok": False, "error": {"type": "invalid_request",
                                           "message": "timeout_ms must be >= 100"}}
        request = json.dumps({"target": self.target, "code": params["code"]})
        sem = work_semaphore or _NoopSem()    # one semaphore shared by ALL heavy tools
        async with sem:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "probe_runner",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(request.encode()),
                    timeout=params.get("timeout_ms", DEFAULT_TIMEOUT_MS) / 1000)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()      # REAP — skipping this leaks zombies
                return {"ok": False, "error": {"type": "timeout"}}
        # Defensive ladder — child output is untrusted:
        if proc.returncode not in (0, None):  # None counts as success-path
            return {"ok": False, "error": {"type": "runner_process_error"},
                    "runner_stdout": out.decode(errors="replace"),
                    "runner_stderr": err.decode(errors="replace")}
        try:
            payload = json.loads(out.decode(errors="replace").strip() or "x")
            assert isinstance(payload, dict) and isinstance(payload.get("ok"), bool)
        except Exception:
            return {"ok": False, "error": {"type": "invalid_runner_output"},
                    "runner_stdout": out.decode(errors="replace")}
        if not params.get("include_stdout"):  # token economy by default
            for k in ("stdout", "stderr"):
                payload.pop(k, None)
        return payload                        # ALL failures in-band, typed

# ---------- child: probe_runner module, fresh interpreter per call ----------
def runner_main():
    import io
    from contextlib import redirect_stdout, redirect_stderr
    try:
        request = json.loads(sys.stdin.read())
        target, code = request["target"], request["code"]
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": {
            "type": "invalid_request", "message": str(exc)}}))
        return 0                              # exit 0 ALWAYS — JSON carries the signal

    emitted, count = None, 0
    def emit(value):                          # exactly-once result channel
        nonlocal emitted, count
        count += 1
        if count > 1:
            raise EmitContractError("emit(value) must be called exactly once")
        emitted = value

    buf_out, buf_err = io.StringIO(), io.StringIO()
    stage = "load"
    try:
        artifact = load_artifact_fresh(target)          # re-execute source each call
        session = InspectionSession(artifact)           # curated helper facade
        ns = {"__name__": "__probe__", "emit": emit, **session.build_namespace()}
        stage = "exec"
        compiled = compile(code, target + ".probe.py", "exec")  # attributable tracebacks
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            exec(compiled, ns, ns)
        if count == 0:
            raise EmitContractError("emit(value) was not called")
        result = jsonable(emitted)
        json.dumps(result)                    # serializability PRE-check, before assembly
        payload = {"ok": True, "result": result,
                   "stdout": buf_out.getvalue(), "stderr": buf_err.getvalue()}
    except LookupFailure as exc:              # unknown-name: message names the bad key
        payload = _err("lookup_failure", exc, buf_out, buf_err)
    except EmitContractError as exc:
        payload = _err("emit_contract", exc, buf_out, buf_err)
    except TypeError as exc:
        kind = ("non_serializable_result" if "not JSON serializable" in str(exc)
                else "snippet_exception")
        payload = _err(kind, exc, buf_out, buf_err, tb=True)
    except Exception as exc:                  # blame the right party via `stage`
        kind = "snippet_exception" if stage == "exec" else "load_failure"
        payload = _err(kind, exc, buf_out, buf_err, tb=True)
    sys.stdout.write(json.dumps(payload))
    return 0

def _err(kind, exc, buf_out, buf_err, tb=False):
    import traceback
    e = {"type": kind, "message": str(exc)}
    if tb:
        e["traceback"] = traceback.format_exc()
    return {"ok": False, "error": e,
            "stdout": buf_out.getvalue(), "stderr": buf_err.getvalue()}

# ---------- helper facade: the ONLY part you rewrite per domain ----------
class InspectionSession:
    def __init__(self, artifact):
        self._index = build_name_indexes(artifact)   # name -> object
        self._cache = {}                             # (id(obj), metric) -> value
        self._cache_refs = {}                        # STRONG refs so ids stay valid

    def lookup(self, name):
        try:
            return self._index[name]
        except KeyError:
            raise LookupFailure(f"Unknown element: {name!r}") from None

    def relation_report(self, a, b):                 # self-describing outputs
        return {"metric_kind": "exact_pair", "a": a, "b": b, "...": "floats only"}

    def scan_all(self, limit=10):                    # O(n^2): sort worst-first, truncate
        ...

    def catalog(self):                               # grouped listing for self-discovery
        ...

    def build_namespace(self):
        return {"lookup": self.lookup, "relation_report": self.relation_report,
                "scan_all": self.scan_all, "catalog": self.catalog}

# Batch sizing: semaphore permits ~= min(cpu_budget,
#   max(1, (nofile_soft_limit - current_open_fds - reserve) // per_worker_fd_budget))
```

## Pitfalls

- **No memory rlimit exists in this design.** Memory safety comes only from short-lived spawn processes releasing everything on exit plus the concurrency cap; a single pathological snippet can still OOM the host. Add `RLIMIT_AS` in the child's preexec if your workload allows it.
- **The exec namespace is not a sandbox.** "Inspection-only" (no writes, no subprocess, no network) is enforced purely by the tool description; the real boundaries are process isolation, parent timeout, and read-only use of the target. Less-trusted code needs OS-level sandboxing (seccomp, containers, unprivileged users) around the runner.
- After `kill()` on timeout, `await proc.communicate()` **again** (`tool.py:79`) — skipping the reap leaks zombies and pipe buffers.
- Run `json.dumps` on the emitted value **before** assembling the response (`runner.py:138`); discovering unserializability while writing the final payload corrupts the single-JSON-on-stdout protocol.
- The runner exits 0 even on error — the parent must branch on payload content, not exit code; and `returncode is None` must be treated as the success path (`tool.py:92`).
- Never trust child stdout: handle empty output, non-JSON, non-dict JSON, and dict-without-bool-`ok` as four distinct malformed cases, each preserving raw child output for diagnosis.
- `id()`-keyed caches need strong references to the keyed objects (`helpers.py:1008`) or GC recycles ids and returns wrong values; invalidate the cache when mutable session state (e.g. pose) changes.
- Pop any legacy path parameter before schema validation (`tool.py:194`) — accepting model-supplied paths lets the LLM probe arbitrary files; bind the target harness-side.
- Track a `stage` variable to distinguish load-time from exec-time exceptions — blaming a broken artifact on the snippet (or vice versa) sends the LLM down the wrong repair path.
- Redirect print capture only around the snippet exec, and route the machine result exclusively through `emit()` — mixing channels makes results ambiguous.
- Default to spawn/forkserver multiprocessing with an env escape hatch; fork from a threaded async runner can deadlock and copies heavy native state into children.
- All-pairs scans are O(n²) over expensive metrics — without per-element caching and worst-first truncation they blow both wall-clock and token budgets.
- FD introspection can fail (no `resource` module, no `/dev/fd` or `/proc/self/fd`) — return `None` and let callers fall back to defaults; clamp the cap to 1 when the budget is exhausted so batches keep making progress instead of erroring.

## Checklist

- [ ] Untrusted/LLM-authored code runs in a freshly spawned subprocess, never in-process `exec()`
- [ ] Parent enforces wall-clock timeout via `wait_for` + `kill()` + a second `communicate()` to reap
- [ ] Runner always exits 0; all failures return in-band as `{ok: false, error: {type, message, traceback?}}`
- [ ] Typed error taxonomy covers: invalid_request, load_failure, lookup_failure, emit_contract, non_serializable_result, snippet_exception, timeout, runner_process_error, invalid_runner_output
- [ ] Defensive parsing ladder on child output: crash / empty / non-JSON / non-dict / bad `ok` field, each preserving raw output
- [ ] `emit()`-exactly-once result channel, separate from stdout; `json.dumps` pre-check before payload assembly
- [ ] `stage` variable attributes load-time vs exec-time failures to the correct party
- [ ] Target file bound by the harness, not chosen by the LLM; legacy path params popped pre-validation
- [ ] Curated helper namespace with precise unknown-name errors, `metric_kind`-tagged reports, `catalog()` self-discovery, and truncated worst-first scans
- [ ] stdout/stderr stripped from the payload by default, opt-in via a flag
- [ ] One shared semaphore caps ALL heavy local subprocess work across parallel rollouts; `None` = unlimited for single runs
- [ ] Semaphore sized from live rlimit introspection (FD budget per worker), clamped to ≥ 1, `None` fallback when unavailable
- [ ] Long native computations (compiles) run in spawn-start `mp.Process` with Pipe + poll-timeout + terminate + bounded join; plain-dict results only
- [ ] Timeout error messages name the env knobs the operator can tune
- [ ] Documented decision on hard sandboxing: prompt-contract-only is acceptable for self-generated code, OS-level isolation required for anything less trusted
