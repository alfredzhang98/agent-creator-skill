"""Shell — the open-world escape hatch, and what it costs.

Ported from Claude Code 2.1.88 ``src/tools/BashTool``. That directory is
~12,400 lines, of which ~8,500 are permission, security and command-parsing
logic rather than execution. **That ratio is the lesson.** Admitting an
arbitrary shell means re-deriving the semantics of shell commands in order to
decide whether a string is read-only, which paths it touches, and whether it is
destructive — forever, for every shell, correctly. If your domain can be served
by three narrow tools instead, take the three tools (see reference 10).

This module ships the *contract and the classification*, not an executor: the
command goes to a ``SandboxBackend`` you supply (``../sandbox_backend.py``).
The default backend refuses, with an actionable message.

The load-bearing coupling to steal, whatever your executor:

    **Isolation buys autonomy.** A command that runs inside the sandbox does
    not need a permission prompt; a command that escapes it always does. The
    sandbox is not a tax on the agent — it is what pays for the agent's
    independence.
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contract import Context, Permission, ToolResult, Validation, build_tool

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000

#: Commands with no side effects outside their own stdout. Deliberately an
#: ALLOWLIST: an unknown command is assumed to write, so a new binary is never
#: silently auto-approved.
READ_ONLY_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "file", "stat", "du", "df", "pwd",
    "echo", "printf", "date", "whoami", "hostname", "uname", "which", "type",
    "grep", "rg", "egrep", "fgrep", "find", "fd", "tree", "sort", "uniq",
    "cut", "awk", "sed", "diff", "cmp", "basename", "dirname", "realpath",
    "env", "printenv", "id", "ps", "top", "free", "uptime",
})

#: Read-only subcommands of tools that are otherwise mutating.
READ_ONLY_SUBCOMMANDS = {
    "git": {"status", "log", "diff", "show", "branch", "remote", "config",
            "blame", "describe", "rev-parse", "ls-files", "stash"},
    "docker": {"ps", "images", "logs", "inspect", "version"},
    "kubectl": {"get", "describe", "logs", "version"},
    "npm": {"ls", "view", "outdated"},
    "pip": {"list", "show", "freeze"},
    "cargo": {"tree", "metadata"},
}

#: Substrings that mark a command as destructive regardless of anything else.
#: This is a *warning* layer, not the security boundary — the sandbox is.
DESTRUCTIVE_PATTERNS = (
    r"\brm\s+(-\w*[rf]|--recursive|--force)",
    r"\bgit\s+(push\s+.*--force|reset\s+--hard|clean\s+-\w*f|branch\s+-D)",
    r"\bdd\s+.*\bof=",
    r"\bmkfs\b", r"\bshred\b", r"\bchown\s+-R\b", r"\bchmod\s+-R\s+777",
    r">\s*/dev/[sh]d", r"\btruncate\s+-s\s*0",
    r":\(\)\s*\{.*\};\s*:",                    # fork bomb
)

_SEPARATORS = re.compile(r"\|\||&&|\||;|\n")


def split_command(command: str) -> list[str]:
    """Split a compound command into its parts.

    Every part is classified independently. A compound command is only as safe
    as its most dangerous element — ``ls && rm -rf /`` must never inherit
    ``ls``'s read-only verdict.
    """
    return [p.strip() for p in _SEPARATORS.split(command) if p.strip()]


def head_of(part: str) -> tuple[str, list[str]]:
    """Return (program, args) with leading env assignments stripped."""
    try:
        tokens = shlex.split(part)
    except ValueError:
        return "", []
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens.pop(0)
    if tokens and tokens[0] in ("sudo", "env", "command", "nohup", "time"):
        tokens.pop(0)
    return (tokens[0] if tokens else ""), tokens[1:]


def is_read_only_command(command: str) -> bool:
    """Conservative: every part must be independently known read-only."""
    parts = split_command(command)
    if not parts:
        return False
    for part in parts:
        if any(ch in part for ch in (">", ">>")):
            return False                            # redirection writes
        prog, args = head_of(part)
        if not prog:
            return False
        base = os.path.basename(prog)
        if base in READ_ONLY_COMMANDS:
            # `find -delete`/`-exec` and in-place sed are the classic escapes.
            if base == "find" and any(a in ("-delete", "-exec", "-execdir") for a in args):
                return False
            if base == "sed" and any(a.startswith("-i") for a in args):
                return False
            continue
        subs = READ_ONLY_SUBCOMMANDS.get(base)
        if subs and args and args[0] in subs:
            continue
        return False                                # unknown => assume it writes
    return True


def is_destructive_command(command: str) -> bool:
    return any(re.search(p, command) for p in DESTRUCTIVE_PATTERNS)


def rule_key_for(command: str) -> str:
    """The string a permission rule scopes to: the first two tokens.

    ``git status`` and ``git push`` are different grants; ``git`` alone is not
    a useful one. Two tokens is the granularity people actually reason about.
    """
    prog, args = head_of(split_command(command)[0] if split_command(command) else command)
    base = os.path.basename(prog)
    if base in READ_ONLY_SUBCOMMANDS or base in ("git", "npm", "docker", "kubectl", "cargo"):
        return f"{base} {args[0]}" if args else base
    return base


def make_shell_tool(backend: Any, policy: Any, allow_sandbox_escape: bool = False) -> Any:
    """Build a Shell tool bound to a ``SandboxBackend`` and ``SandboxPolicy``.

    *allow_sandbox_escape* controls whether the model may request an
    unsandboxed run at all. When false, the parameter does not exist and the
    tool says so — a capability the operator disabled should not appear in the
    schema at all.
    """

    def _validate(input_: dict[str, Any], ctx: Context) -> Validation:
        command = input_.get("command", "")
        if not command.strip():
            return Validation.invalid("command must be a non-empty string")
        try:
            shlex.split(split_command(command)[0])
        except ValueError as exc:
            return Validation.invalid(f"Command is not parseable: {exc}")
        timeout = input_.get("timeout")
        if timeout is not None and not (0 < timeout <= MAX_TIMEOUT_MS):
            return Validation.invalid(
                f"timeout must be between 1 and {MAX_TIMEOUT_MS} ms (got {timeout})"
            )
        if input_.get("dangerouslyDisableSandbox") and not allow_sandbox_escape:
            return Validation.invalid(
                "Unsandboxed execution is disabled by policy. If a command fails "
                "because of a sandbox restriction, report the restriction rather "
                "than trying to bypass it."
            )
        return Validation.valid()

    def _check(input_: dict[str, Any], ctx: Context) -> Permission:
        command = input_["command"]
        if is_destructive_command(command):
            # Destructive commands ask even when a rule would allow the prefix,
            # and the reason type makes the decision bypass-immune.
            return Permission(
                Permission.ASK,
                message=f"This command is destructive and cannot be undone:\n  {command}",
                reason={"type": "safety", "detail": "destructive command"},
            )
        if input_.get("dangerouslyDisableSandbox"):
            return Permission(
                Permission.ASK,
                message=f"Run OUTSIDE the sandbox?\n  {command}",
                reason={"type": "safety", "detail": "sandbox escape"},
            )
        # Isolation buys autonomy: inside the sandbox, no prompt.
        if getattr(policy, "network_disabled", False) and getattr(
            policy, "isolated_workspace", False
        ):
            return Permission(
                Permission.ALLOW, reason={"type": "sandbox", "detail": "runs sandboxed"}
            )
        return Permission(Permission.PASSTHROUGH)

    def _call(input_: dict[str, Any], ctx: Context) -> ToolResult:
        command = input_["command"]
        timeout_ms = int(input_.get("timeout") or DEFAULT_TIMEOUT_MS)
        run_policy = policy
        if hasattr(policy, "__class__"):
            try:
                run_policy = type(policy)(
                    **{**policy.__dict__, "wall_timeout_s": timeout_ms / 1000}
                )
            except Exception:  # noqa: BLE001 - frozen dataclass variants differ
                run_policy = policy

        res = backend.run(ctx.cwd, ["/bin/sh", "-lc", command], run_policy)

        # Everything the backend can return is already typed; translate, never
        # raise. A refusing or crashing sandbox must read as a tool error the
        # model can route around, not as a broken conversation.
        if getattr(res, "ok", False):
            out = (res.stdout or "").rstrip()
            err = (res.stderr or "").rstrip()
            body = out or "(no output)"
            if err:
                body += f"\n\n[stderr]\n{err}"
            return ToolResult.success(body, data={"duration_s": res.duration_s})
        return ToolResult.failure(
            f"{res.error_type or 'error'}: {res.error or 'command failed'}"
            + (f"\n\n[stdout]\n{res.stdout.rstrip()}" if res.stdout else "")
            + (f"\n\n[stderr]\n{res.stderr.rstrip()}" if res.stderr else ""),
            code=res.error_type or "shell_error",
        )

    props: dict[str, Any] = {
        "command": {"type": "string", "description": "The command to execute"},
        "timeout": {
            "type": "integer",
            "description": f"Timeout in ms (max {MAX_TIMEOUT_MS})",
        },
        "description": {
            "type": "string",
            "description": "5-10 words, active voice, describing what it does",
        },
    }
    if allow_sandbox_escape:
        props["dangerouslyDisableSandbox"] = {
            "type": "boolean",
            "description": "Run outside the sandbox. Requires explicit approval.",
        }

    return build_tool(
        name="Shell",
        description=(
            "Execute a shell command inside an isolated sandbox and return its "
            "output.\n\n"
            f"- Default timeout {DEFAULT_TIMEOUT_MS // 1000}s, max {MAX_TIMEOUT_MS // 1000}s.\n"
            "- The working directory persists between calls; shell state does not.\n"
            "- Prefer the dedicated tools where one fits: Read over `cat`, Edit over "
            "`sed -i`, Write over `cat <<EOF`, Grep over `grep`, Glob over `find`. "
            "They are reviewable, capped, and permissioned; a shell string is none "
            "of those.\n"
            "- If a command fails because of a sandbox restriction, say which "
            "restriction — do not work around it silently."
        ),
        search_hint="execute shell commands",
        input_schema={
            "type": "object",
            "properties": props,
            "required": ["command"],
            "additionalProperties": False,
        },
        call=_call,
        validate_input=_validate,
        check_permissions=_check,
        # All three are functions of THIS command, not of the tool.
        is_read_only=lambda i: is_read_only_command(i.get("command", "")),
        is_concurrency_safe=lambda i: is_read_only_command(i.get("command", "")),
        is_destructive=lambda i: is_destructive_command(i.get("command", "")),
        is_open_world=lambda _i: True,
        max_result_chars=30_000,
        rule_key=lambda i: rule_key_for(i.get("command", "")),
        activity=lambda i: i.get("description") or f"Running {i.get('command','')[:40]}",
    )
