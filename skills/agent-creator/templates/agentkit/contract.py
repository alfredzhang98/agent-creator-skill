"""The tool contract: one declaration per tool, fail-closed defaults in one place.

Distilled from Claude Code 2.1.88 ``src/Tool.ts`` (the ~40-member ``Tool`` type
and ``buildTool``) and Articraft ``agent/tools/base.py`` (declarative params,
errors-as-data).

Two ideas carry almost all the weight:

1. **Safety predicates are functions of the INPUT, not properties of the tool.**
   ``shell("ls")`` is read-only; ``shell("rm -rf /")`` is destructive. The
   harness derives concurrency, permission strictness and UI treatment from the
   tool's own answers, so it never needs a table of tool names.

2. **Defaults are fail-closed and live in exactly one place** (``build_tool``).
   A tool author who forgets ``is_read_only`` gets "assume it writes", and no
   call site anywhere writes ``getattr(tool, "x", default)``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Protocol

JSONSchema = dict[str, Any]

# System-wide ceiling on a tool result before it is persisted to disk instead
# of being inlined (Claude Code: constants/toolLimits.ts:13).
DEFAULT_MAX_RESULT_CHARS = 50_000


# --------------------------------------------------------------------------
# Results: every failure is data, never an exception.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolResult:
    """What a tool hands back. Exactly one of ok/error is meaningful."""

    ok: bool
    content: str = ""
    error: str = ""
    #: Machine-readable classification, e.g. "not_found", "not_unique".
    code: str = ""
    #: Structured payload for programmatic consumers (never sent verbatim).
    data: Any = None
    #: Free-form notes that do not change ok/error (e.g. lint status).
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, content: str, **kw: Any) -> "ToolResult":
        return cls(ok=True, content=content, **kw)

    @classmethod
    def failure(cls, error: str, code: str = "error", **kw: Any) -> "ToolResult":
        return cls(ok=False, error=error, code=code, **kw)

    def to_message(self, tool_use_id: str) -> dict[str, Any]:
        """Render as an API tool_result block.

        Errors are wrapped in ``<tool_use_error>`` and flagged ``is_error`` so
        the model can tell a failure from a result that merely mentions one
        (Claude Code: services/tools/toolExecution.ts:664-679).
        """
        if self.ok:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": self.content,
            }
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": True,
            "content": f"<tool_use_error>{self.error}</tool_use_error>",
        }


@dataclass(frozen=True)
class Validation:
    """Result of cheap, pre-permission input validation."""

    ok: bool
    message: str = ""
    code: int = 0

    @classmethod
    def valid(cls) -> "Validation":
        return cls(ok=True)

    @classmethod
    def invalid(cls, message: str, code: int = 1) -> "Validation":
        return cls(ok=False, message=message, code=code)


@dataclass(frozen=True)
class Permission:
    """A permission decision about one specific call.

    ``passthrough`` means "the tool has no opinion" and is converted to ``ask``
    by the permission layer — fail-closed by construction
    (Claude Code: utils/permissions/permissions.ts:1299-1310).
    """

    behavior: str  # 'allow' | 'deny' | 'ask' | 'passthrough'
    message: str = ""
    #: Why: {'type': 'rule'|'mode'|'hook'|'safety'|'sandbox', ...}
    reason: dict[str, Any] = field(default_factory=dict)
    #: Rules the UI can offer as one-click grants when behavior == 'ask'.
    suggestions: tuple[dict[str, Any], ...] = ()
    #: A tool may narrow/normalise its own input as a condition of running.
    updated_input: dict[str, Any] | None = None

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    PASSTHROUGH = "passthrough"


class Context(Protocol):
    """Everything a tool may read from its environment.

    Kept deliberately small: a tool that needs more is usually a tool that
    should be two tools.
    """

    cwd: str
    session_dir: str
    read_files: dict[str, float]   # abs path -> mtime when last read
    permission_mode: str
    aborted: Callable[[], bool]


# --------------------------------------------------------------------------
# The tool itself.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Tool:
    """A complete tool. Build these with :func:`build_tool`, never directly."""

    name: str
    description: str
    input_schema: JSONSchema
    call: Callable[[dict[str, Any], Context], ToolResult]

    # --- discovery -------------------------------------------------------
    aliases: tuple[str, ...] = ()
    #: 3-10 words used for keyword matching when the schema is deferred.
    search_hint: str = ""
    #: True => ship the name only; the schema is fetched via tool_search.
    should_defer: bool = False
    #: True => never defer, even when deferral is on. Use for the search tool
    #: itself and for tools whose description carries a turn-1 contract.
    always_load: bool = False

    # --- semantics, as functions of the INPUT ----------------------------
    is_read_only: Callable[[dict[str, Any]], bool] = lambda _i: False
    is_concurrency_safe: Callable[[dict[str, Any]], bool] = lambda _i: False
    is_destructive: Callable[[dict[str, Any]], bool] = lambda _i: False
    #: True when the tool reaches outside the trust boundary (network, etc.);
    #: its output must be treated as untrusted input.
    is_open_world: Callable[[dict[str, Any]], bool] = lambda _i: False
    #: True when the call cannot proceed without a human, even in bypass mode.
    requires_user_interaction: Callable[[], bool] = lambda: False
    is_enabled: Callable[[], bool] = lambda: True

    # --- gating ----------------------------------------------------------
    validate_input: Callable[[dict[str, Any], Context], Validation] = (
        lambda _i, _c: Validation.valid()
    )
    check_permissions: Callable[[dict[str, Any], Context], Permission] = (
        lambda i, _c: Permission(Permission.PASSTHROUGH)
    )
    #: Per-tool result cap, clamped by ``DEFAULT_MAX_RESULT_CHARS``. Declaring
    #: anything ABOVE that default is a no-op — only values below it, and the
    #: explicit ``math.inf`` opt-out, actually bind. The opt-out is correct only
    #: for tools that already self-bound AND whose result would create a
    #: read-the-file-back loop (Claude Code: Tool.ts:457-466).
    max_result_chars: float = DEFAULT_MAX_RESULT_CHARS

    # --- observability ---------------------------------------------------
    #: Mutate a COPY of the input to add derived fields for observers (hooks,
    #: transcript, permission UI). The copy never reaches ``call`` — the model's
    #: original bytes must survive for prompt-cache and transcript stability
    #: (Claude Code: Tool.ts:474-481).
    backfill_observable_input: Callable[[dict[str, Any]], None] | None = None
    #: One short line for a spinner: "Reading src/foo.py".
    activity: Callable[[dict[str, Any]], str] | None = None
    #: The substring a permission rule scopes to for THIS call — the command
    #: for a shell tool, the path for a file tool. Distinct from `activity`,
    #: which is for humans: this one is matched against rules, so it must be
    #: stable and derived only from the input. ``None`` means the tool can be
    #: matched by blanket rules only.
    rule_key: Callable[[dict[str, Any]], str | None] | None = None

    def matches(self, name: str) -> bool:
        return name == self.name or name in self.aliases

    def to_api_schema(self, defer: bool = False) -> dict[str, Any]:
        """Render for the model.

        A deferred tool still sends its full definition plus
        ``defer_loading: true`` — the *server* is what withholds the schema
        from the model. Dropping ``input_schema`` client-side is a 400.
        """
        schema = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
        if defer:
            schema["defer_loading"] = True
        return schema


#: Predicates whose omission must fail CLOSED. The values live on the
#: dataclass itself — this is only the list of which fields are safety
#: relevant, so ``build_tool`` can assert they exist rather than re-declare
#: them. Keeping one copy is the point: a second table drifts.
_FAIL_CLOSED_FIELDS = (
    "is_read_only",            # assume it writes
    "is_concurrency_safe",     # assume it races
    "is_destructive",
    "is_open_world",
    "requires_user_interaction",
    "validate_input",
    # `passthrough` is NOT `allow`: the permission layer converts a tool with
    # no opinion into `ask`. See permissions.decide step 3.
    "check_permissions",
)


def build_tool(**kwargs: Any) -> Tool:
    """Construct a Tool. Omitted safety predicates keep their fail-closed
    dataclass defaults.

    Prefer this over ``Tool(...)`` so there is one documented construction
    path, and so this assertion runs (Claude Code: Tool.ts:743-792).
    """
    unknown = set(kwargs) - set(Tool.__dataclass_fields__)
    if unknown:
        raise TypeError(f"unknown Tool field(s): {', '.join(sorted(unknown))}")
    tool = Tool(**kwargs)
    for field_name in _FAIL_CLOSED_FIELDS:
        if getattr(tool, field_name) is None:
            raise ValueError(f"{tool.name}: {field_name} must not be None")
    return tool


def with_overrides(tool: Tool, **kwargs: Any) -> Tool:
    """Return a copy of *tool* with fields replaced (for aliases/wrappers)."""
    return replace(tool, **kwargs)


def find_tool(tools: Iterable[Tool], name: str) -> Tool | None:
    for t in tools:
        if t.matches(name):
            return t
    return None


def effective_result_cap(tool: Tool) -> float:
    """The cap actually applied: the tool's own, clamped by the global default.

    ``math.inf`` survives the clamp — that is the documented opt-out.
    """
    if math.isinf(tool.max_result_chars):
        return math.inf
    return min(tool.max_result_chars, DEFAULT_MAX_RESULT_CHARS)
