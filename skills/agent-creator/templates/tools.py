"""Declarative tool framework: schema -> validated invocation -> data-only result.

Distills ``articraft.agent.tools._core`` (tool base, params validation,
invocation lifecycle, resource binding) and the concrete tools around it
(edit/read/write/compile).  Nothing raises across the tool boundary: every
failure becomes a ToolResult that the loop serializes back to the LLM.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResult:
    """Exactly one of output/error; side_channel carries advisory status."""

    output: Any = None
    error: str | None = None
    side_channel: dict[str, Any] | None = None   # e.g. {"syntax_ok": False}

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"error": self.error} if self.error else {"result": self.output}
        if self.side_channel:
            d["validation"] = self.side_channel
        return d


class ParamError(ValueError):
    """Raised by validation; dispatch converts it into a teaching error string."""


_JSON_TYPES: dict[type, str] = {str: "string", int: "integer", float: "number",
                                bool: "boolean", list: "array", dict: "object"}


@dataclass(frozen=True)
class Param:
    """One declared parameter; the schema is generated from these."""

    name: str
    type: type = str
    required: bool = True
    description: str = ""
    default: Any = None


def validate_params(params: tuple[Param, ...], raw: dict[str, Any]) -> dict[str, Any]:
    """Strict validation: extra keys forbidden; explicit None means 'omitted'."""
    raw = {k: v for k, v in raw.items() if v is not None}
    known = {p.name for p in params}
    extra = sorted(set(raw) - known)
    if extra:
        raise ParamError(f"unknown parameter(s) {extra}; expected {sorted(known)}")
    out: dict[str, Any] = {}
    for p in params:
        if p.name not in raw:
            if p.required:
                raise ParamError(f"missing required parameter {p.name!r}")
            out[p.name] = p.default
            continue
        value = raw[p.name]
        if (isinstance(value, bool) and p.type in (int, float)) \
                or not isinstance(value, p.type):
            raise ParamError(f"parameter {p.name!r} must be "
                             f"{_JSON_TYPES.get(p.type, p.type.__name__)}")
        out[p.name] = value
    return out


class Invocation:
    """Phase 2 object: typed params + runtime context.  execute() never raises."""

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params

    def describe(self) -> str:
        """Loggable one-line preview, emitted BEFORE any side effect."""
        return f"{type(self).__name__}({self.params})"

    def execute(self) -> ToolResult:
        try:
            return self._run()
        except Exception as exc:  # boundary: errors become data, never bubble
            return ToolResult(error=f"tool execution error: {exc}")

    def _run(self) -> ToolResult:  # override with the tool body
        raise NotImplementedError


class BoundResourceInvocation(Invocation):
    """The LLM never names real paths: the harness injects handles post-build."""

    resource: Any = None

    def bind_resource(self, handle: Any) -> None:
        self.resource = handle


@dataclass
class DeclarativeTool:
    """Phase 1 object: stateless, carries name + schema, builds invocations."""

    name: str
    description: str
    params: tuple[Param, ...] = ()
    invocation_cls: type[Invocation] = Invocation
    mutating: bool = False   # mutating tools stale the verify cache (verifier.py)

    def schema(self) -> dict[str, Any]:
        props = {p.name: {"type": _JSON_TYPES.get(p.type, "string"),
                          "description": p.description} for p in self.params}
        return {"name": self.name, "description": self.description,
                "input_schema": {"type": "object", "properties": props,
                                 "required": [p.name for p in self.params if p.required],
                                 "additionalProperties": False}}

    def build(self, raw: dict[str, Any]) -> Invocation:
        return self.invocation_cls(validate_params(self.params, raw))


class Registry:
    """Name -> tool; dispatch turns any failure into an error ToolResult message."""

    def __init__(self, tools: list[DeclarativeTool],
                 context: dict[str, Any] | None = None) -> None:
        self._tools = {t.name: t for t in tools}
        self._context = context or {}   # e.g. {"resource": workfile_handle}

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def is_mutating(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.mutating)

    def dispatch(self, call: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Return (tool message for the conversation, success flag)."""
        result = self._execute(call.get("name", ""), call)
        message = {"role": "tool", "tool_call_id": call.get("id"),
                   "content": json.dumps(result.to_dict())}
        return message, result.ok

    def _execute(self, name: str, call: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(error=f"tool {name!r} not found; "
                                    f"available: {sorted(self._tools)}")
        raw = call.get("arguments") or {}
        if isinstance(raw, str):        # most providers send a JSON string
            try:
                raw = json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                return ToolResult(error=f"arguments for {name!r} are not valid JSON: {exc}")
        try:
            invocation = tool.build(raw)
        except ParamError as exc:       # teach the model, do not just reject
            return ToolResult(error=f"invalid parameters for {name!r}: {exc}. "
                                    f"Provided: {sorted(raw)}")
        for hook, value in self._context.items():   # duck-typed injection
            setter = getattr(invocation, f"bind_{hook}", None)
            if callable(setter):
                setter(value)
        return invocation.execute()


# Pattern for mutating invocations (see the articraft edit tool): three layers.
#   1. HARD invariant gate (AST/structural): refuse the write, list what is missing.
#   2. ADVISORY validity check (compile/lint): write anyway, report via side_channel.
#   3. Edit safety: old text must exist and be unique, else report the match count
#      and suggest a longer snippet — error strings state the corrective ACTION.
