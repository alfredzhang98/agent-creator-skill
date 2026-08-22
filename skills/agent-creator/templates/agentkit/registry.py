"""Assembling the tool pool the model actually sees.

Distilled from Claude Code 2.1.88 ``src/tools.ts`` (pool assembly, deny
filtering, cache-stable ordering), ``src/utils/toolSearch.ts`` and
``src/tools/ToolSearchTool/prompt.ts`` (deferred schemas).

Three decisions live here, and all three are about tokens rather than
capability:

1. **Filter before assembly.** A tool the model may not use should not appear
   at all — not appear and then get rejected.
2. **Order is a cache decision.** Built-ins sort as a contiguous prefix ahead
   of dynamically discovered tools, so connecting a new external server cannot
   interleave into the stable region and invalidate everything after it.
3. **Defer schemas above a threshold.** Below it, the extra round-trip costs
   more than the schemas it saves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from contract import Tool, find_tool
from permissions import PermissionContext, deny_filtered

#: Defer when deferrable schemas exceed this share of the context window.
AUTO_DEFER_CONTEXT_FRACTION = 0.10
#: Tool schemas are JSON-dense; prose ratios overestimate how many fit.
CHARS_PER_TOKEN = 2.5


@dataclass
class Pool:
    """The assembled, ordered tool set for one request.

    ``loaded`` is mutable and load-bearing: it is how a deferred tool stops
    being deferred once its schema has been fetched. Without it the search
    tool hands the model a schema and the dispatcher still refuses the call —
    a livelock that burns the whole turn budget.
    """

    tools: tuple[Tool, ...]
    deferred: frozenset[str]
    #: Names whose schema the model has fetched this session.
    loaded: set[str] = field(default_factory=set)

    def is_withheld(self, name: str) -> bool:
        """True when the model has NOT been given this tool's schema yet."""
        return name in self.deferred and name not in self.loaded

    def mark_loaded(self, names: Iterable[str]) -> None:
        self.loaded.update(names)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.to_api_schema(defer=self.is_withheld(t.name)) for t in self.tools]

    def get(self, name: str) -> Tool | None:
        return find_tool(self.tools, name)

    @property
    def withheld_names(self) -> list[str]:
        return [t.name for t in self.tools if self.is_withheld(t.name)]


def assemble(
    builtin: Sequence[Tool],
    external: Sequence[Tool] = (),
    *,
    permission_ctx: PermissionContext | None = None,
    context_window_tokens: int | None = None,
    defer_mode: str = "auto",       # 'auto' | 'always' | 'never'
) -> Pool:
    """Build the pool: filter, order, then decide what ships as a bare name."""
    ctx = permission_ctx or PermissionContext()
    allowed_builtin = [t for t in deny_filtered(builtin, ctx) if t.is_enabled()]
    allowed_external = [t for t in deny_filtered(external, ctx) if t.is_enabled()]

    # Sort each partition independently and keep built-ins as a contiguous
    # prefix. A flat sort would let an external tool sort *between* two
    # built-ins, moving every schema after it and busting the prompt cache.
    ordered: list[Tool] = sorted(allowed_builtin, key=lambda t: t.name)
    seen = {t.name for t in ordered}
    for t in sorted(allowed_external, key=lambda t: t.name):
        if t.name not in seen:     # built-ins win on name collision
            ordered.append(t)
            seen.add(t.name)

    return Pool(tuple(ordered), _deferred_set(ordered, context_window_tokens, defer_mode))


def _deferrable(tool: Tool, has_search_tool: bool) -> bool:
    if not has_search_tool:
        return False              # never strand a schema with no way to fetch it
    if tool.always_load:
        return False
    if tool.name == TOOL_SEARCH_NAME:
        return False              # the loader can never be deferred
    return tool.should_defer


def _deferred_set(
    tools: Sequence[Tool], context_window_tokens: int | None, mode: str
) -> frozenset[str]:
    has_search = any(t.name == TOOL_SEARCH_NAME for t in tools)
    candidates = [t for t in tools if _deferrable(t, has_search)]
    if mode == "never" or not candidates:
        return frozenset()
    if mode == "always":
        return frozenset(t.name for t in candidates)

    # auto: only worth a round-trip once the schemas are actually expensive.
    if not context_window_tokens:
        return frozenset()
    chars = sum(len(t.description) + len(str(t.input_schema)) for t in candidates)
    if chars / CHARS_PER_TOKEN > context_window_tokens * AUTO_DEFER_CONTEXT_FRACTION:
        return frozenset(t.name for t in candidates)
    return frozenset()


def announce_deferred(pool: Pool) -> str:
    """The message that tells the model which tools exist but are not loaded.

    Bare names only. Rendering each tool's one-line hint here was A/B-tested
    upstream and showed no benefit while costing tokens on every single turn
    (Claude Code: tools/ToolSearchTool/prompt.ts:110-117).
    """
    names = pool.withheld_names
    if not names:
        return ""
    return (
        "The following tools are available but their schemas are not loaded. "
        f"Use {TOOL_SEARCH_NAME} to fetch a schema before calling one:\n"
        + "\n".join(f"- {n}" for n in names)
    )


# --------------------------------------------------------------------------
# The search tool itself.
# --------------------------------------------------------------------------

TOOL_SEARCH_NAME = "ToolSearch"


def search(pool: Pool, query: str, max_results: int = 5) -> tuple[list[Tool], list[str]]:
    """Resolve a query against the withheld set.

    Returns ``(matches, already_loaded)``. The second list matters: a model
    asking for a tool it can already call needs to be told that, not told
    "no match" — otherwise it searches again.

    Three query forms, because the model knows different amounts at different
    times: an exact list when it knows the names, a required term when it knows
    the family, and keywords when it only knows the job.
    """
    withheld = [t for t in pool.tools if pool.is_withheld(t.name)]
    q = query.strip()

    if q.startswith("select:"):
        wanted = [n.strip() for n in q[len("select:"):].split(",") if n.strip()]
        matches, loaded = [], []
        for name in wanted:
            if (t := find_tool(withheld, name)) is not None:
                matches.append(t)
            elif find_tool(pool.tools, name) is not None:
                loaded.append(name)
        return matches, loaded

    required: list[str] = []
    terms: list[str] = []
    for token in re.split(r"\s+", q.lower()):
        if not token:
            continue
        (required if token.startswith("+") else terms).append(token.lstrip("+"))

    def score(t: Tool) -> int:
        hay = f"{t.name} {t.search_hint} {t.description}".lower()
        if any(r not in t.name.lower() for r in required):
            return -1
        # A required term that matched is itself evidence, so a query of only
        # required terms still scores. Otherwise "+slack" can never match.
        return (
            sum(2 for r in required)
            + sum(3 if term in t.name.lower() else 1 for term in terms if term in hay)
        )

    ranked = sorted(((score(t), t) for t in withheld), key=lambda p: -p[0])
    return [t for sc, t in ranked if sc > 0][:max_results], []
