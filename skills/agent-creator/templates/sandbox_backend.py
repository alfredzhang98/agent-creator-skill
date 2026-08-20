"""Execution contract for generated code: a typed sandbox interface.

This template deliberately ships NO execution engine — no ``exec``, no
``eval``, no process spawning. It distills the *contracts* around executing
model-generated code (from articraft's probe/compile supervision protocol:
``agent/tools/probe_model/tool.py``, ``agent/compiler.py``) into three pieces:

1. ``SandboxPolicy``   — the minimum security bar, expressed as data.
2. ``SandboxBackend``  — the interface any real execution backend implements.
3. ``validate_child_payload`` — the defensive parsing ladder for whatever the
   backend returns (child output is untrusted input, always).

The design rule this file encodes:

    A child process is a reliability boundary, not a security sandbox.

Process supervision (timeouts, kill-and-reap, rlimits) keeps the agent loop
alive when generated code hangs or crashes. It does NOT stop generated code
from reading the filesystem, using the network, or inheriting secrets. Real
isolation comes from an OS-level backend: a container with networking
disabled, a microVM (e.g. Firecracker), gVisor, or a remote sandbox service.
Pick one per deployment and implement ``SandboxBackend`` for it; the default
``UnconfiguredSandbox`` refuses to run anything and says why.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

# Closed error taxonomy: every failure the agent loop may see, as data.
# (Distilled from articraft's probe runner taxonomy — typed, in-band,
#  actionable by the LLM on the next turn.)
ERROR_TYPES = (
    "sandbox_not_configured",   # no real backend wired in
    "policy_violation",         # requested run violates SandboxPolicy
    "invalid_request",          # malformed run request
    "timeout",                  # wall-clock limit hit; child killed
    "crash",                    # child died (nonzero exit / signal)
    "invalid_payload",          # child output failed the parsing ladder
    "artifact_error",           # generated code raised; traceback attached
)


@dataclass(frozen=True)
class SandboxPolicy:
    """The minimum security bar for executing generated code, as data.

    Defaults are the SAFE values. A backend must refuse to run when it
    cannot honor a field — silently downgrading is how "sandboxes" become
    plain subprocesses.
    """

    # Security isolation — what makes it a sandbox:
    network_disabled: bool = True        # no outbound/inbound network
    isolated_workspace: bool = True      # fresh tmp dir; host fs invisible
    inherit_host_env: bool = False       # no env vars / secrets leak into child
    run_as_root: bool = False            # unprivileged user inside the sandbox
    # Reliability limits — what keeps the loop alive:
    wall_timeout_s: float = 300.0        # hard kill on expiry (parent-enforced)
    cpu_seconds: int = 60                # kills CPU-bound runaways
    memory_bytes: int = 2_000_000_000
    max_open_files: int = 256
    max_processes: int = 32              # blocks fork bombs

    def violations(self) -> list[str]:
        """Fields currently weaker than the safe default, for refuse/log."""
        out = []
        if not self.network_disabled:
            out.append("network is enabled")
        if not self.isolated_workspace:
            out.append("host filesystem is visible")
        if self.inherit_host_env:
            out.append("host environment/secrets are inherited")
        if self.run_as_root:
            out.append("runs as root")
        return out


@dataclass
class SandboxResult:
    """One typed result for every outcome — the loop never needs try/except."""

    ok: bool
    result: Any = None                   # parsed artifact/output on success
    error_type: str = ""                 # one of ERROR_TYPES when not ok
    error: str = ""
    stdout: str = ""                     # tail-truncated by the backend
    stderr: str = ""
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SandboxBackend(Protocol):
    """Interface a real execution backend implements.

    Contract (distilled from articraft's supervision protocol):
    - Enforce ``policy.wall_timeout_s`` in the PARENT; on expiry, kill the
      sandbox and reap it (a second wait) so nothing leaks.
    - Never raise for child misbehavior: map every failure to a
      ``SandboxResult`` with a type from ``ERROR_TYPES``.
    - Treat all child output as untrusted — run it through
      ``validate_child_payload`` (or equivalent) before returning it.
    - Refuse (``policy_violation``) rather than downgrade when the
      environment cannot honor a ``SandboxPolicy`` field.

    Reference backends to implement per deployment: container with
    ``--network=none`` and a tmpfs workspace; a microVM (Firecracker);
    gVisor; or a remote sandbox service. See references/04.
    """

    def run(
        self,
        artifact_dir: str,
        entrypoint: list[str],
        policy: SandboxPolicy,
    ) -> SandboxResult:
        """Run ``entrypoint`` against ``artifact_dir`` under ``policy``."""
        ...


@dataclass
class UnconfiguredSandbox:
    """Default backend: refuses to execute anything, with an actionable error.

    Shipping a refusing default instead of a permissive one is deliberate:
    an agent template must not double as a general-purpose code-execution
    utility. Wire a real backend before the verifier can run generated code.
    """

    hint: str = field(default=(
        "Generated-code execution requires an OS-isolated sandbox backend "
        "(container with networking disabled, microVM, gVisor, or a remote "
        "sandbox service). Implement SandboxBackend.run() for your "
        "deployment and pass it to the verifier."
    ))

    def run(
        self,
        artifact_dir: str,
        entrypoint: list[str],
        policy: SandboxPolicy,
    ) -> SandboxResult:
        return SandboxResult(
            ok=False, error_type="sandbox_not_configured", error=self.hint,
        )


def validate_child_payload(raw: str, returncode: int | None) -> SandboxResult:
    """Defensive parsing ladder for child output — untrusted input, always.

    Protocol expectations (child side): write exactly ONE JSON object to
    stdout and exit 0 even on failure — the JSON carries the signal, the
    exit code is reserved for genuine crashes. Parent side (this ladder):
    branch on crash / empty / non-JSON / non-dict / missing-bool-``ok``,
    preserving raw output for diagnosis at every step.
    """
    if returncode not in (0, None):      # None counts as the success path
        return SandboxResult(
            ok=False, error_type="crash",
            error=f"sandbox exited with code {returncode}",
            stdout=raw[-4000:],
        )
    if not raw.strip():
        return SandboxResult(
            ok=False, error_type="invalid_payload",
            error="sandbox produced no output",
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return SandboxResult(
            ok=False, error_type="invalid_payload",
            error="sandbox output was not valid JSON",
            stdout=raw[-4000:],
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return SandboxResult(
            ok=False, error_type="invalid_payload",
            error="sandbox output was not a result object",
            stdout=raw[-4000:],
        )
    return SandboxResult(
        ok=payload["ok"],
        result=payload.get("result"),
        error_type=str(payload.get("error_type", "")),
        error=str(payload.get("error", "")),
        stdout=str(payload.get("stdout", ""))[-4000:],
        stderr=str(payload.get("stderr", ""))[-4000:],
    )
