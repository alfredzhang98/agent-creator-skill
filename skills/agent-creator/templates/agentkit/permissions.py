"""Consent: who may do what, decided per call.

Distilled from Claude Code 2.1.88 ``src/utils/permissions/permissions.ts``
and ``src/types/permissions.ts``.

This module is the answer to "what says no?" when the domain admits no
mechanical verifier. Four properties make it trustworthy rather than
theatrical:

1. **Two layers, not one.** An inner ladder decides on the merits; an outer
   wrapper then applies mode transformations that must not be reachable by an
   early return. Collapsing these into one pass is how "skip the prompts"
   quietly becomes "grant everything" — see ``decide`` vs ``resolve``.
2. **Ordered, numbered, and total.** Every call exits at exactly one labelled
   step, and the decision carries which one.
3. **Some decisions are bypass-immune.** Explicit denies, calls that
   inherently need a human, and safety checks survive every mode.
4. **Unknown means ask.** A tool with no opinion returns ``passthrough``,
   which step 3 converts to ``ask``. There is no path where silence means yes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from contract import Context, Permission, Tool

#: Modes, least to most autonomous — except `dont_ask`, which is the most
#: RESTRICTIVE autonomous mode: it converts every `ask` into a `deny` so an
#: unattended run never blocks. It is not "bypass lite"; do not treat it so.
MODES = ("plan", "default", "accept_edits", "dont_ask", "bypass")

#: `decisionReason` types that survive bypass mode. Spellings must match what
#: tools actually emit — a typo here silently disables immunity, so
#: `decide` asserts on unknown reason types rather than ignoring them.
BYPASS_IMMUNE_REASONS = frozenset({"rule_deny", "rule_ask", "safety", "safetyCheck"})

#: Every reason type a tool may put on a Permission. Closed so that a
#: misspelled type is a loud error instead of a silently-lost guarantee.
KNOWN_REASON_TYPES = BYPASS_IMMUNE_REASONS | frozenset({
    "rule_allow", "mode", "hook", "sandbox", "user", "default", "error", "",
})

#: Rule sources, most authoritative first. A project-level allow must never
#: outrank a policy-level deny just because it appears earlier in a list.
SOURCE_PRECEDENCE = ("policy", "cli", "user", "project", "local", "session", "command")


@dataclass(frozen=True)
class Rule:
    """``tool`` alone matches the whole tool; ``content`` scopes it.

    ``content`` supports an exact match and a trailing ``:*`` prefix form
    (``git:*``). Deliberately NOT fnmatch: a user typing ``a?b`` means the
    literal string, and silently widening a hand-written rule into a glob
    grants more than they asked for.
    """

    tool: str
    behavior: str            # 'allow' | 'deny' | 'ask'
    content: str | None = None
    source: str = "session"

    def matches(self, tool_name: str, rule_key: str | None) -> bool:
        if self.tool != tool_name:
            return False
        if self.content is None:
            return True          # blanket rule for the whole tool
        if rule_key is None:
            # A content-scoped rule cannot be evaluated without a rule key.
            # Callers must not treat that as "no match" — see `unscopable`.
            return False
        if self.content.endswith(":*"):
            return rule_key.startswith(self.content[:-2])
        return rule_key == self.content

    @property
    def rank(self) -> int:
        return (
            SOURCE_PRECEDENCE.index(self.source)
            if self.source in SOURCE_PRECEDENCE
            else len(SOURCE_PRECEDENCE)
        )


@dataclass
class PermissionContext:
    mode: str = "default"
    rules: list[Rule] = field(default_factory=list)
    #: Set when the session STARTED in bypass mode; plan mode inherits it.
    bypass_available: bool = False
    #: Tools whose writes `accept_edits` may auto-approve. Upstream resolves
    #: this inside each tool's own checkPermissions; an explicit allowlist is
    #: the honest port, because "is this an edit?" is not derivable from the
    #: generic predicates.
    edit_tools: frozenset[str] = frozenset()
    #: Consecutive denials per tool; crossing the limit escalates to `ask`
    #: so an agent cannot grind silently against a wall.
    denials: dict[str, int] = field(default_factory=dict)
    denial_limit: int = 3

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown permission mode {self.mode!r}; expected one of {MODES}")

    def matching(self, behavior: str, tool_name: str, key: str | None) -> Rule | None:
        """Highest-authority matching rule, not merely the first in the list."""
        hits = [r for r in self.rules if r.behavior == behavior and r.matches(tool_name, key)]
        return min(hits, key=lambda r: r.rank) if hits else None

    def unscopable(self, behavior: str, tool_name: str, key: str | None) -> Rule | None:
        """A content-scoped rule that exists but cannot be evaluated.

        If someone configured ``Bash(rm:*) deny`` and the tool supplies no
        rule key, the honest outcome is `ask` — never `allow`. Treating an
        unevaluable deny as absent is how configured denies evaporate.
        """
        if key is not None:
            return None
        for r in self.rules:
            if r.behavior == behavior and r.tool == tool_name and r.content is not None:
                return r
        return None


def deny_filtered(tools: Iterable[Tool], ctx: PermissionContext) -> list[Tool]:
    """Drop blanket-denied tools BEFORE the request is assembled."""
    return [t for t in tools if ctx.matching("deny", t.name, None) is None]


# --------------------------------------------------------------------------
# Layer 1: the ladder. Decides on the merits, knows nothing about modes that
# transform the *outcome*.
# --------------------------------------------------------------------------

def decide(
    tool: Tool,
    input_: dict[str, Any],
    ctx: PermissionContext,
    context: Context,
    rule_key: str | None = None,
) -> Permission:
    """Run the inner ladder. Exactly one step decides; the reason names it.

    Callers should use :func:`resolve`, which applies this and then the outer
    mode transformations. This function is exposed separately because hooks
    and pre-checks legitimately need the rule-based subset on its own.
    """
    # 1a. Whole tool, or matching content, denied.
    if (r := ctx.matching("deny", tool.name, rule_key)) is not None:
        return Permission(
            Permission.DENY,
            message=f"Permission to use {tool.name} has been denied.",
            reason={"type": "rule_deny", "step": "1a", "rule": r},
        )
    # 1a'. A content-scoped deny we cannot evaluate must not become an allow.
    if (r := ctx.unscopable("deny", tool.name, rule_key)) is not None:
        return Permission(
            Permission.ASK,
            message=(
                f"{tool.name} has a content-scoped deny rule ({r.content!r}) but "
                "supplies no rule key, so it cannot be evaluated automatically."
            ),
            reason={"type": "rule_ask", "step": "1a'", "rule": r},
        )

    # 1b. Whole tool always asks.
    if (r := ctx.matching("ask", tool.name, None)) is not None:
        return Permission(
            Permission.ASK,
            message=f"Allow {tool.name}?",
            reason={"type": "rule_ask", "step": "1b", "rule": r},
        )

    # 1c. Ask the tool itself. A tool that raises is treated as having no
    #     opinion — a broken predicate must never become an implicit allow.
    try:
        result = tool.check_permissions(input_, context)
    except Exception as exc:  # noqa: BLE001 - deliberately total
        result = Permission(
            Permission.PASSTHROUGH, reason={"type": "error", "detail": str(exc)}
        )
    rtype = result.reason.get("type", "")
    if rtype not in KNOWN_REASON_TYPES:
        raise ValueError(
            f"{tool.name} returned unknown permission reason type {rtype!r}. "
            f"Known: {sorted(KNOWN_REASON_TYPES)}. An unrecognised type would "
            "silently lose bypass immunity, so this is an error, not a warning."
        )

    # 1d. The tool denied: final, and immune to mode.
    if result.behavior == Permission.DENY:
        return result
    # 1e. The call needs a human regardless of mode.
    if result.behavior == Permission.ASK and tool.requires_user_interaction():
        return result
    # 1f/1g. Content-scoped asks and safety checks outrank bypass mode.
    if result.behavior == Permission.ASK and rtype in BYPASS_IMMUNE_REASONS:
        return result

    # 2a. Bypass mode allows everything that survived the immune classes.
    if ctx.mode == "bypass" or (ctx.mode == "plan" and ctx.bypass_available):
        return Permission(
            Permission.ALLOW,
            reason={"type": "mode", "step": "2a", "mode": ctx.mode},
            updated_input=result.updated_input,
        )

    # 2b. An explicit allow rule.
    if (r := ctx.matching("allow", tool.name, rule_key)) is not None:
        return Permission(
            Permission.ALLOW,
            reason={"type": "rule_allow", "step": "2b", "rule": r},
            updated_input=result.updated_input,
        )

    # 2c. accept_edits: a standing grant for THIS deployment's edit tools.
    #     Scoped by an explicit allowlist rather than derived from predicates,
    #     and never covering destructive calls.
    if (
        ctx.mode == "accept_edits"
        and tool.name in ctx.edit_tools
        and not tool.is_destructive(input_)
    ):
        return Permission(
            Permission.ALLOW,
            reason={"type": "mode", "step": "2c", "mode": ctx.mode},
            updated_input=result.updated_input,
        )

    # 2d. The tool affirmatively allowed and nothing above objected.
    if result.behavior == Permission.ALLOW:
        return result

    # 3. Unknown means ask. This is the only default.
    return Permission(
        Permission.ASK,
        message=result.message or f"Allow {tool.name}?",
        reason=result.reason if rtype else {"type": "default", "step": "3"},
        suggestions=result.suggestions or suggest_rules(tool.name, rule_key),
        updated_input=result.updated_input,
    )


# --------------------------------------------------------------------------
# Layer 2: outer transformations. Applied AFTER the ladder so no early return
# can skip them.
# --------------------------------------------------------------------------

def resolve(
    tool: Tool,
    input_: dict[str, Any],
    ctx: PermissionContext,
    context: Context,
    rule_key: str | None = None,
) -> Permission:
    """The decision callers should use: ladder, then mode transformations."""
    decision = decide(tool, input_, ctx, context, rule_key)

    # dont_ask means "never block an unattended run" — which is ask → DENY,
    # not ask → allow. Applied last so it cannot be bypassed by an early
    # return, mirroring the upstream comment at permissions.ts:473-520.
    if decision.behavior == Permission.ASK and ctx.mode == "dont_ask":
        return Permission(
            Permission.DENY,
            message=(
                f"{tool.name} needs approval, and this session runs with "
                "dont_ask, so it is refused rather than prompted."
            ),
            reason={"type": "mode", "step": "outer", "mode": "dont_ask",
                    "inner": decision.reason},
        )

    # Repeated denials escalate to an explicit ask: an agent retrying a denied
    # action forever looks fine in the logs and empties the budget.
    if decision.behavior == Permission.DENY:
        n = ctx.denials.get(tool.name, 0) + 1
        ctx.denials[tool.name] = n
        if n >= ctx.denial_limit and ctx.mode != "dont_ask":
            return Permission(
                Permission.ASK,
                message=(
                    f"{tool.name} has been denied {n} times. Approve it, or tell "
                    "the agent to stop trying."
                ),
                reason={"type": "rule_ask", "step": "outer",
                        "detail": "denial limit", "inner": decision.reason},
            )
    else:
        ctx.denials.pop(tool.name, None)
    return decision


def suggest_rules(tool_name: str, rule_key: str | None) -> tuple[dict[str, Any], ...]:
    """One-click grants offered alongside an ``ask``.

    The prefix suggestion keeps the first TWO tokens (``git push:*``), not the
    first one: ``git:*`` would cover ``git push --force``, which is exactly the
    over-broad grant the narrow option exists to avoid.
    """
    if rule_key is None:
        return ({"tool": tool_name, "content": None, "scope": "tool"},)
    parts = rule_key.split()
    prefix = " ".join(parts[:2]) if len(parts) > 1 else parts[0]
    out = [{"tool": tool_name, "content": rule_key, "scope": "exact"}]
    if prefix != rule_key:
        out.append({"tool": tool_name, "content": f"{prefix}:*", "scope": "prefix"})
    return tuple(out)
