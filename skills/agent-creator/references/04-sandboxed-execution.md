# 04. Executing Generated Code: Isolation & Supervision

**Maps to:** Tools · Guardrails · Evaluator/Verifier · Executor · Cost · **Distilled from:** Articraft `agent/tools/probe_model/{tool.py,runner.py,helpers.py}`, `agent/mp_utils.py`, `agent/runtime_limits.py`, `agent/open_file_limits.py`, `agent/compiler.py`

> **The one rule in this document:**
> **A child process is a reliability boundary, not a security sandbox.**
> Process supervision keeps your agent loop alive when generated code hangs
> or crashes. It does nothing to stop that code from reading your
> filesystem, reaching the network, or inheriting your secrets. Those
> require an OS-level boundary. Do not conflate the two.

## Why this module exists

Agents that build code artifacts eventually need to *run* code — the artifact itself (compile/verify) or model-authored inspection snippets against it ("measure the gap between these two parts"). This is untrusted, model-authored input executing on your machine, and it fails in two independent dimensions. It can **hang, segfault, or exhaust memory**, killing the agent run (a *reliability* problem). And it can **read secrets, write outside its workspace, or exfiltrate over the network** (a *security* problem). These need different mechanisms, and the most common architectural mistake in agent codebases is shipping the first and calling it the second. This document separates them: the security boundary you must obtain from the OS, and the supervision protocol you build on top of it so failures come back as typed data the model can self-correct from.

## The two boundaries

| | Reliability isolation | Security isolation |
|---|---|---|
| **Answers** | "Will a bad artifact kill my run?" | "Can a bad artifact hurt me?" |
| **Mechanisms** | separate process · wall-clock timeout · kill-and-reap · CPU/memory/FD rlimits · concurrency cap | filesystem isolation · network disabled · privilege drop (non-root) · syscall filtering · env/secret scrubbing · process-count cap |
| **Provided by** | your supervision code (this document) | container · microVM (Firecracker) · gVisor · remote sandbox service |
| **If missing** | one bad snippet ends the run | one bad snippet owns the host |

`rlimit` + `subprocess` + `timeout` sits entirely in the left column. A prompt instruction saying "inspection only — do not write files or use the network" is **not** in either column: **prompt constraints are not a security boundary.** Untrusted generated code must only execute inside an OS-isolated sandbox.

Minimum security bar before you execute model-authored code:

```
network:      disabled by default
filesystem:   fresh isolated workspace; host filesystem inaccessible
env/secrets:  not inherited
user:         non-root
limits:       CPU, memory, process-count, wall-clock timeout
```

If your backend cannot honor one of these, **refuse to run and say so** — silently downgrading is how a "sandbox" becomes a plain subprocess.

## How Articraft implements it

Articraft is a useful case study for the **supervision protocol** — the part that turns crashes and hangs into structured LLM feedback. Read this section for that protocol. Note explicitly what it is *not*: Articraft executes its own agent's generated CAD scripts under process isolation and prompt contract only, with no OS-level sandbox. That is a defensible tradeoff for a single-user tool running self-generated code on a trusted machine; it is **not** a model to copy for less-trusted input. The `SandboxBackend` seam in the reusable pattern below is where you insert the missing boundary.

### Parent-side launch: fresh interpreter, hard timeout, kill-and-reap

`execute()` serializes a request dict `{file_path, sdk_package, code}` to JSON, then spawns a fresh interpreter with `asyncio.create_subprocess_exec(sys.executable, "-m", "agent.tools.probe_model.runner")`, cwd pinned to the repo root (`agent/tools/probe_model/tool.py:47-89`). The request goes to the child's stdin; the reply is read from stdout via `process.communicate()` wrapped in `asyncio.wait_for(timeout_ms/1000)`. On `asyncio.TimeoutError` the parent calls `process.kill()` then awaits `communicate()` **again** to reap the zombie (`tool.py:78-79`), returning an in-band `{ok: false, error: {type: "timeout"}}` payload rather than raising — the LLM sees the timeout as data and can narrow its probe. Timeout enforcement lives entirely in the parent because model-authored child code cannot be trusted to self-limit. The launch is gated by a shared local-work semaphore (`tool.py:62`), and `timeout_ms` is validated `>= 100ms` up front (`tool.py:50-51`).

### Defensive parsing ladder: child output is untrusted

The parent never assumes the child produced valid output (`tool.py:90-152`). It branches, in order: nonzero returncode → `runner_process_error` with raw stdout/stderr attached (this is how a segfault in a native CAD library surfaces as data); empty stdout → `invalid_runner_output`; `json.loads` failure → same; payload not a dict → same; `payload["ok"]` not a bool → malformed. Note `returncode not in (0, None)` — `None` is treated as the success path (`tool.py:92`). Unless the caller passed `include_stdout=true`, the keys `stdout`/`stderr`/`runner_stdout`/`runner_stderr` are popped before returning (`tool.py:147-151`): debug output is captured but costs tokens only on request.

### Runner protocol: exit 0 always, JSON carries the signal

The child's `main()` reads the whole request from stdin; even a malformed request produces `{ok: false, error.type: "invalid_request"}` and **exit code 0** (`agent/tools/probe_model/runner.py:92-98`). The exit code never carries error signal — only genuine crashes hit the parent's returncode branch. The runner loads the artifact fresh under a module lock with chdir to the script directory (`agent/compiler.py:372-391`), requires an `object_model` global, builds the helper session, and runs the snippet under a synthetic filename `file_path.with_suffix(".probe.py")` so tracebacks are attributable (`runner.py:132`). A `stage` variable flips from `"load"` to `"exec"` immediately before the snippet runs, so exceptions are blamed on the correct party: artifact → `load_failure`, snippet → `snippet_exception` (`runner.py:145-188`).

### emit()-exactly-once result channel with serializability pre-check

A closure-based `emit(value)` raises on a second call, and `emit_count == 0` afterwards also raises (`runner.py:103-108,135-136`). The emitted value is normalized by `_jsonable` (recursing dicts/lists/tuples, special-casing domain pose objects, `runner.py:25-37`), then `json.dumps(value)` runs as a **pure pre-check** before payload assembly (`runner.py:138`) so a `TypeError` maps to a dedicated `non_serializable_result` error instead of corrupting the single-JSON-on-stdout protocol mid-write. `print()` output goes to separate StringIO buffers — machine channel and debug channel never mix.

### Typed error taxonomy the LLM can act on

Every failure classifies into one of: `invalid_request`, `load_failure` (with traceback), `lookup_failure` (unknown name — the message names the bad key), `emit_contract`, `non_serializable_result`, `snippet_exception` (with traceback), plus parent-side `timeout`, `runner_process_error`, `invalid_runner_output` (`runner.py:145-188`, `tool.py:90-152`). Because `ProbeLookupError` carries the exact unknown name (`helpers.py:165-181`), the LLM gets "Unknown part: X" instead of a raw KeyError traceback — precise, self-correctable feedback that keeps the loop alive instead of aborting it.

### Harness-bound target: the LLM never chooses the path

The tool schema exposes no path parameter. `ProbeModelInvocation` extends `BoundFileToolInvocation` (`agent/tools/base.py:118-126`); the harness injects the session's current artifact via `bind_file_path()` after building any invocation (`agent/harness.py:984-986`). `build()` pops a legacy `file_path` key from LLM params before validation (`tool.py:192-203`), so the model cannot target arbitrary files. This is a genuine confinement win and it is cheap — but note it constrains *which* file the tool opens, not what the executed code may touch once running.

### Killable compile worker: spawn-first multiprocessing

The sibling heavy workload — URDF compilation — uses `mp.Process` + one-way `Pipe`: the parent polls with `URDF_COMPILE_TIMEOUT_SECONDS` (default 300s, env-tunable, 0 disables) and on expiry terminates the worker, joins with 2.0s grace, and raises a TimeoutError naming the env knobs to tune (`agent/compiler.py:618-700`). Start method is spawn-first, then forkserver, with an `ARTICRAFT_MP_START_METHOD` override (`agent/mp_utils.py:23-48`): fork from a long-lived threaded async runner risks deadlocks and drags heavy native state into children. Results cross the pipe as plain dicts, so nothing unpicklable must survive process death.

### Shared local-work semaphore and FD-budget sizing

`BatchRuntimeLimits` holds one optional `asyncio.Semaphore`; `local_work_slot(limits)` yields immediately when limits is `None` (single-run = unlimited) and otherwise acquires (`agent/runtime_limits.py:9-28`). The **same** object is threaded into the harness, all compile paths, and every probe tool instance (`agent/harness.py:204`, `agent/tools/__init__.py:90-107`), so total concurrent heavy subprocesses across N parallel rollouts is capped by a single number. For sizing, `open_file_worker_cap()` reads `RLIMIT_NOFILE`, counts open FDs via `/dev/fd` (falling back to `/proc/self/fd`), computes `max(1, (soft_limit - open_files - reserve) // per_worker_budget)`, clamps to 1 when usable ≤ 0, and returns `None` when introspection is unavailable so callers fall back to defaults (`agent/open_file_limits.py:21-69`).

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Security boundary is an OS-level backend behind an interface, never bespoke in-process filtering | Import blocklists and prompt rules are trivially bypassable; containers/microVMs/gVisor are audited boundaries maintained by specialists | Deployment dependency; a backend must be chosen and configured per environment |
| Default backend refuses to execute and returns an actionable error | An agent template must not double as a general-purpose code-execution utility; failing closed makes the missing boundary visible at integration time, not after an incident | Extra wiring step before the verify loop can run at all |
| Refuse rather than downgrade when the environment can't honor a policy field | Silent downgrade is exactly how "sandbox" degrades into "subprocess" without anyone noticing | Some environments simply cannot run the verifier |
| Fresh interpreter per call, stdin-JSON / stdout-JSON | Only a process is reliably killable, converts segfaults to parseable errors, returns all memory on exit; re-executing source means probes always see the current artifact, never stale state | Per-call startup + full artifact re-execution; mitigated by generous timeout and semaphore gating |
| All failures in-band as `{ok:false, error:{type,...}}`; child always exits 0 | The consumer is an LLM: typed errors enable self-correction; a raised tool exception aborts the loop; exit code stays reserved for genuine crashes | Parent needs a five-step defensive parsing ladder |
| `emit()`-exactly-once, separate from stdout, with `json.dumps` pre-check | Deterministic machine-parseable result — no guessing which printed line is the answer; late TypeError becomes a dedicated fixable error type | A new contract the model can violate; state it in the tool description |
| Curated pre-injected helper namespace instead of raw library access | Stable documented API with `catalog()` self-discovery; precise unknown-name errors; narrows what a snippet naturally reaches for | Helper layer couples to internals and must track refactors |
| No path param; harness binds the target, legacy param popped pre-validation | The tool inspects the artifact under construction; model-chosen paths invite reading arbitrary files | Cannot compare two files; multi-file flows need explicit rebinding |
| Strip stdout/stderr by default (`include_stdout=false`) | Debug prints allowed without flooding LLM context — direct token-cost control | A misbehaving snippet may need a second call to see its own prints |
| Spawn-first mp start method; Pipe + poll-timeout + terminate for compiles | Fork-with-threads deadlocks; spawn gives clean short-lived workers whose native memory is fully reclaimed | Spawn pays import cost per worker |
| One shared semaphore for ALL heavy local work, FD-based sizing | A single cap protects the host regardless of which tool triggers work; `None` = zero overhead in single runs | No per-tool priority — a slow compile can delay a cheap probe |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| Policy defaults (safe) | network off · isolated workspace · env not inherited · non-root | The minimum bar; a backend that cannot honor these must refuse |
| `wall_timeout_s` | 300 s | Parent-enforced hard kill; the artifact's own limits are never trusted |
| `cpu_seconds` / `memory_bytes` / `max_open_files` / `max_processes` | 60 s / 2 GB / 256 / 32 | Reliability rlimits; process cap blocks fork bombs |
| `DEFAULT_PROBE_TIMEOUT_MS` | 600_000 (10 min) | Each call re-executes the full artifact build; per-call overridable (`tool.py:20`) |
| `timeout_ms` minimum | 100 ms | Rejects timeouts that would kill the child before startup (`tool.py:50-51`) |
| `include_stdout` default | `false` | Token economy: captured, returned only on request (`tool.py:26,147-151`) |
| `URDF_COMPILE_TIMEOUT_SECONDS` | 300 s (env; 0 disables) | Hard cap on the compile worker; message names tuning knobs (`compiler.py:632`) |
| Worker join grace | 2.0 s | Bounded wait after `terminate()` before giving up on a clean exit (`compiler.py:668,680,684`) |
| Start-method preference | `("spawn", "forkserver")` | Isolates heavy native state; avoids fork-with-threads hazards (`mp_utils.py:23-48`) |
| Payload/stdout truncation | 4000 chars (tail) | Bounds token cost of a chatty or crashing child |
| FD worker cap formula | `max(1, (soft - open - reserve) // per_worker)` | Sizes concurrency to RLIMIT_NOFILE; clamp to 1 keeps batches progressing (`open_file_limits.py:57-61`) |

## Reusable pattern

The template is the *contract*, not an execution engine: `templates/sandbox_backend.py` ships no `exec`, `eval`, or process spawning. You supply the OS boundary.

```python
# The seam: policy (data) + backend (interface) + defensive result parsing.
from dataclasses import dataclass
from typing import Any, Protocol

ERROR_TYPES = ("sandbox_not_configured", "policy_violation", "invalid_request",
               "timeout", "crash", "invalid_payload", "artifact_error")


@dataclass(frozen=True)
class SandboxPolicy:
    """Minimum security bar as DATA. Defaults are the safe values."""
    network_disabled: bool = True       # security boundary ...
    isolated_workspace: bool = True
    inherit_host_env: bool = False
    run_as_root: bool = False
    wall_timeout_s: float = 300.0       # ... reliability limits
    cpu_seconds: int = 60
    memory_bytes: int = 2_000_000_000
    max_processes: int = 32


@dataclass
class SandboxResult:
    ok: bool
    result: Any = None
    error_type: str = ""                # one of ERROR_TYPES
    error: str = ""
    stdout: str = ""
    stderr: str = ""


class SandboxBackend(Protocol):
    """Implement once per deployment: container (--network=none + tmpfs
    workspace), microVM, gVisor, or a remote sandbox service.

    Contract: enforce the timeout in the PARENT (kill, then reap); never
    raise for child misbehavior — map everything to a typed SandboxResult;
    treat child output as untrusted; REFUSE rather than downgrade when the
    environment cannot honor a policy field."""

    def run(self, artifact_dir: str, entrypoint: list[str],
            policy: SandboxPolicy) -> SandboxResult: ...


class UnconfiguredSandbox:
    """Ship this as the default. Fail closed, with an actionable message."""

    def run(self, artifact_dir, entrypoint, policy) -> SandboxResult:
        return SandboxResult(
            ok=False, error_type="sandbox_not_configured",
            error=("Generated-code execution requires an OS-isolated sandbox "
                   "backend (container with networking disabled, microVM, "
                   "gVisor, or a remote sandbox service)."))


def validate_child_payload(raw: str, returncode: int | None) -> SandboxResult:
    """Untrusted input, always. Child protocol: exactly ONE JSON object on
    stdout, exit 0 even on failure — JSON carries the signal, the exit code
    is reserved for genuine crashes."""
    import json
    if returncode not in (0, None):          # None counts as success-path
        return SandboxResult(False, error_type="crash",
                             error=f"sandbox exited {returncode}",
                             stdout=raw[-4000:])
    if not raw.strip():
        return SandboxResult(False, error_type="invalid_payload",
                             error="sandbox produced no output")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return SandboxResult(False, error_type="invalid_payload",
                             error="not valid JSON", stdout=raw[-4000:])
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return SandboxResult(False, error_type="invalid_payload",
                             error="not a result object", stdout=raw[-4000:])
    return SandboxResult(ok=payload["ok"], result=payload.get("result"),
                         error_type=str(payload.get("error_type", "")),
                         error=str(payload.get("error", "")))


# Verifier-side usage — the loop never needs try/except:
def verify(artifact_dir, backend: SandboxBackend, policy=SandboxPolicy()):
    if bad := [f for f in policy.violations()]:      # refuse, don't downgrade
        return SandboxResult(False, error_type="policy_violation",
                             error="; ".join(bad))
    result = backend.run(artifact_dir, ["python", "verify.py"], policy)
    return result            # typed either way -> straight into signal bundle

# Batch sizing: semaphore permits ~= min(cpu_budget,
#   max(1, (nofile_soft - open_fds - reserve) // per_worker_fd_budget))
```

## Pitfalls

- **Do not call process isolation a sandbox.** Timeouts and rlimits are a reliability boundary. Without OS-level filesystem/network/privilege isolation, generated code can still read your `~/.ssh`, POST your environment somewhere, or write anywhere your user can.
- **Prompt constraints are not a security boundary.** "Inspection only — no writes, no network, no subprocess" in a tool description is a hint to a cooperative model, not an enforcement mechanism. Anything less trusted than your own agent's output needs the OS boundary.
- **Fail closed on missing configuration.** A default backend that "just runs it locally" turns every integration into an unnoticed security downgrade. Refuse and name what is missing.
- **A container is not automatically isolated.** Default Docker networking is on and `--privileged`/host mounts undo the boundary. Verify: `--network=none`, non-root user, read-only host mounts (or none), tmpfs workspace, dropped capabilities, no inherited env.
- **Add a process-count limit.** `RLIMIT_NPROC` (or the container equivalent) — without it a fork bomb takes the host down regardless of CPU and memory caps.
- **Memory limits are easy to forget.** Rlimits set in-process apply only to that process tree; enforce memory at the sandbox layer too, or a single snippet OOMs the host.
- After `kill()` on timeout, wait on the process **again** (`tool.py:79`) — skipping the reap leaks zombies and pipe buffers.
- Run `json.dumps` on the result **before** assembling the response (`runner.py:138`); discovering unserializability while writing the payload corrupts the single-JSON-on-stdout protocol.
- The child exits 0 even on error — branch on payload content, not exit code; and `returncode is None` must be treated as the success path (`tool.py:92`).
- Never trust child stdout: handle empty, non-JSON, non-dict JSON, and dict-without-bool-`ok` as four distinct malformed cases, each preserving raw output for diagnosis.
- Pop any legacy path parameter before schema validation (`tool.py:194`) — accepting model-supplied paths lets the LLM target arbitrary files.
- Track a `stage` variable to distinguish load-time from exec-time exceptions — blaming a broken artifact on the snippet (or vice versa) sends the LLM down the wrong repair path.
- Default to spawn/forkserver multiprocessing with an env escape hatch; fork from a threaded async runner can deadlock and copies heavy native state into children.
- FD introspection can fail (no `resource` module, no `/dev/fd` or `/proc/self/fd`) — return `None`, let callers fall back to defaults, and clamp the cap to 1 so batches keep progressing.

## Checklist

**Security boundary (before executing anything model-authored)**

- [ ] An OS-level `SandboxBackend` is configured: container / microVM / gVisor / remote service
- [ ] Network disabled by default
- [ ] Fresh isolated workspace; host filesystem inaccessible
- [ ] Host environment and secrets not inherited
- [ ] Runs as a non-root, unprivileged user
- [ ] Process-count limit set (fork-bomb protection)
- [ ] The backend **refuses** rather than downgrades when a policy field cannot be honored
- [ ] The default backend fails closed with an actionable message
- [ ] No prompt instruction is relied on as an enforcement mechanism

**Reliability supervision (so failures become feedback, not crashes)**

- [ ] Parent enforces the wall-clock timeout: kill, then a second wait to reap
- [ ] CPU, memory, and open-file limits applied at the sandbox layer
- [ ] Child always exits 0; all failures in-band as `{ok:false, error:{type, message, traceback?}}`
- [ ] Typed error taxonomy covers: sandbox_not_configured, policy_violation, invalid_request, timeout, crash, invalid_payload, artifact_error
- [ ] Defensive parsing ladder on child output: crash / empty / non-JSON / non-dict / bad `ok`, each preserving raw output
- [ ] Single-result channel (`emit()`-exactly-once) separate from stdout, with a `json.dumps` pre-check
- [ ] `stage` variable attributes load-time vs run-time failures to the correct party
- [ ] Target bound by the harness, not chosen by the LLM; legacy path params popped pre-validation
- [ ] stdout/stderr truncated and stripped from the payload by default, opt-in via a flag
- [ ] One shared semaphore caps ALL heavy sandbox work across parallel rollouts; sized from live rlimit introspection
- [ ] Timeout error messages name the env knobs an operator can tune
