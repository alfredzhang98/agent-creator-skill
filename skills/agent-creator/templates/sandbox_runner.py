"""Run untrusted generated code in a resource-limited subprocess.

Distills ``articraft.agent._child_process`` and ``articraft.compiler.worker``
(killable verify/probe subprocesses) plus the exec-tool plumbing in
``articraft.agent.tools`` (_exec, exec_command): fresh interpreter per call,
POSIX rlimits, hard timeout with kill-and-reap, and a single JSON result pipe
whose failures are typed data — the parent never raises on child misbehavior.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class Limits:
    cpu_seconds: int = 60                # RLIMIT_CPU: kills CPU-bound runaways
    memory_bytes: int = 2_000_000_000    # RLIMIT_AS
    open_files: int = 256                # RLIMIT_NOFILE


@dataclass
class SandboxResult:
    ok: bool
    result: Any = None
    error_type: str = ""   # timeout|crash|invalid_payload|invalid_request|
    #                        emit_contract|non_serializable_result|snippet_exception
    error: str = ""
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------ parent side ---------------------------------
def run_sandboxed(code: str, *, target: str | None = None,
                  timeout_s: float = DEFAULT_TIMEOUT_S,
                  limits: Limits = Limits(),
                  include_stdout: bool = False) -> SandboxResult:
    """Execute ``code`` in a fresh interpreter; every failure is in-band, typed."""
    request = {"code": code, "target": target, "limits": asdict(limits)}
    proc = subprocess.Popen([sys.executable, __file__, "--child"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(json.dumps(request), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()               # reap: never leak a zombie
        return SandboxResult(False, error_type="timeout",
                             error=f"killed after {timeout_s:.0f}s")
    # Defensive ladder — child output is untrusted.
    if proc.returncode not in (0, None):
        return SandboxResult(False, error_type="crash",
                             error=f"child exited {proc.returncode}",
                             stderr=err[-4000:])
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return SandboxResult(False, error_type="invalid_payload",
                             error="child did not produce a valid result object",
                             stdout=out[-2000:], stderr=err[-2000:])
    result = SandboxResult(ok=payload["ok"], result=payload.get("result"),
                           error_type=str(payload.get("error_type", "")),
                           error=str(payload.get("error", "")),
                           stdout=str(payload.get("stdout", "")),
                           stderr=str(payload.get("stderr", "")))
    if not include_stdout:               # token economy by default
        result.stdout = result.stderr = ""
    return result


# ------------------------------- child side ---------------------------------
def _apply_rlimits(limits: dict[str, int]) -> None:
    """POSIX only; on platforms without ``resource`` the sandbox is weaker."""
    try:
        import resource
    except ImportError:
        return
    resource.setrlimit(resource.RLIMIT_CPU,
                       (limits["cpu_seconds"], limits["cpu_seconds"]))
    resource.setrlimit(resource.RLIMIT_AS,
                       (limits["memory_bytes"], limits["memory_bytes"]))
    resource.setrlimit(resource.RLIMIT_NOFILE,
                       (limits["open_files"], limits["open_files"]))


def _child_main() -> int:
    """Read one JSON request on stdin, write one JSON payload, exit 0 ALWAYS."""
    def done(payload: dict[str, Any]) -> int:
        sys.stdout.write(json.dumps(payload))
        return 0

    try:
        request = json.loads(sys.stdin.read())
        code: str = request["code"]
    except Exception:
        return done({"ok": False, "error_type": "invalid_request",
                     "error": "stdin was not a valid request object"})
    _apply_rlimits(request.get("limits") or asdict(Limits()))

    emitted: list[Any] = []

    def emit(value: Any) -> None:        # exactly-once result channel
        if emitted:
            raise RuntimeError("emit() must be called exactly once")
        emitted.append(value)

    # The snippet sees only what the harness binds — never raw host paths.
    namespace: dict[str, Any] = {"__name__": "__sandbox__", "emit": emit,
                                 "TARGET": request.get("target")}
    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        compiled = compile(code, "<sandboxed>", "exec")
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            exec(compiled, namespace, namespace)
        if not emitted:
            raise RuntimeError("emit(result) was never called")
        json.dumps(emitted[0])           # serializability PRE-check
    except Exception as exc:             # classify: blame the right party
        if isinstance(exc, RuntimeError) and "emit(" in str(exc):
            kind = "emit_contract"
        elif isinstance(exc, TypeError) and "not JSON serializable" in str(exc):
            kind = "non_serializable_result"
        else:
            kind = "snippet_exception"
        return done({"ok": False, "error_type": kind, "error": str(exc),
                     "traceback": traceback.format_exc()[-4000:],
                     "stdout": buf_out.getvalue()[-4000:],
                     "stderr": buf_err.getvalue()[-4000:]})
    return done({"ok": True, "result": emitted[0],
                 "stdout": buf_out.getvalue()[-4000:],
                 "stderr": buf_err.getvalue()[-4000:]})


if __name__ == "__main__" and "--child" in sys.argv:
    sys.exit(_child_main())
