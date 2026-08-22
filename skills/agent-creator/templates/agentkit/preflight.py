"""Prove the agent can talk to the API before it tries to do work.

A generated agent fails in one of two places. Either it fails on turn 40 with
a 404 for a model that never existed, after an hour and a full context window
of spend — or it fails in the first second, saying which model IDs the account
actually has. This module exists to make it the second one.

The failure that motivated it is not exotic, it is the default:

    An agent generator asked for ``gpt-5.6``. No such model. The account had
    ``gpt-5.6-luna``, ``gpt-5.6-sol`` and ``gpt-5.6-terra``.

Note what went wrong, because it is not "the name was slightly off". A model
that *writes* an agent is choosing a model ID from memory — from a training
cut-off that is always behind the provider's catalogue. It cannot know which
models exist today, it cannot know which the account is entitled to, and it
has no way to tell those two apart. Every agent it writes therefore contains a
guess presented as a fact.

    **Never write a model ID from memory. Ask the provider, then match.**

And when the match is ambiguous, refuse:

    **Never auto-pick a variant.** ``gpt-5.6`` matching three siblings is not
    a typo to be corrected — those three differ in price, latency, context and
    capability. Silently taking the first is how an agent ends up quietly
    running on a model nobody chose, and the bill is the notification.

Everything here is pure. Listing models and pinging them is network, and
network belongs to the host — the same seam as ``SandboxBackend`` (reference
04) and ``SkillIndex`` (reference 16). ``UnconfiguredCatalog`` refuses and
prints the exact ``curl`` to run by hand.
"""
from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence

#: Where each provider publishes its catalogue, and the env var holding the
#: key. Endpoints are data so a self-hosted or proxied deployment can override
#: them without editing code.
CATALOGUE = {
    "anthropic": {
        "url": "https://api.anthropic.com/v1/models",
        "key_env": "ANTHROPIC_API_KEY",
        "auth": "x-api-key: {key}",
        "extra": "anthropic-version: 2023-06-01",
    },
    "openai": {
        "url": "https://api.openai.com/v1/models",
        "key_env": "OPENAI_API_KEY",
        "auth": "Authorization: Bearer {key}",
        "extra": "",
    },
    "google": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models",
        "key_env": "GEMINI_API_KEY",
        "auth": "x-goog-api-key: {key}",
        "extra": "",
    },
}

EXACT, AMBIGUOUS, MISSING, UNVERIFIED = "exact", "ambiguous", "missing", "unverified"

#: A trailing variant suffix: "gpt-5.6-luna" -> family "gpt-5.6", variant "luna".
#: Deliberately loose — providers name variants after moons, sizes, dates and
#: nothing at all, so the shape is "family plus one more segment", not a list
#: of known words that would be stale the day it was written.
_VARIANT = re.compile(r"^(?P<family>.+?)-(?P<variant>[a-z0-9]+)$", re.I)


def family_of(model_id: str) -> str:
    """The part before the trailing variant segment, or the id itself."""
    m = _VARIANT.match(model_id)
    return m.group("family") if m else model_id


# ---------------------------------------------------------------------------
# 1. The catalogue (untrusted input)
# ---------------------------------------------------------------------------

def parse_model_list(payload: Any) -> list[str]:
    """Model IDs out of any of the three catalogue shapes, order preserved.

    OpenAI-compatible ``{"data": [{"id": ...}]}``, Anthropic's identical shape,
    Google's ``{"models": [{"name": "models/gemini-..."}]}``, and a plain list
    of strings for proxies that simplify. Anything unrecognised yields ``[]``
    rather than a partial guess: an empty catalogue is honestly reported as
    UNVERIFIED, while a misparsed one would be reported as MISSING and send
    someone hunting for a model that is right there.
    """
    rows: Iterable[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("data") or payload.get("models") or []
        if not isinstance(rows, list):
            return []
    else:
        return []

    out: list[str] = []
    for row in rows:
        ident: Any = None
        if isinstance(row, str):
            ident = row
        elif isinstance(row, dict):
            ident = row.get("id") or row.get("name") or row.get("model")
        if not isinstance(ident, str) or not ident.strip():
            continue
        ident = ident.strip()
        if ident.startswith("models/"):        # Google prefixes the resource path
            ident = ident[len("models/"):]
        if ident not in out:
            out.append(ident)
    return out


# ---------------------------------------------------------------------------
# 2. Resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Resolution:
    """What the requested model ID turned out to be."""

    requested: str
    status: str                              # EXACT | AMBIGUOUS | MISSING | UNVERIFIED
    resolved: str = ""                       # only ever set when status is EXACT
    candidates: tuple[str, ...] = ()         # variants, or nearest names
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == EXACT

    def raise_for_status(self) -> str:
        if self.ok:
            return self.resolved
        raise ModelNotResolved(self.message)


class ModelNotResolved(RuntimeError):
    """Raised on a model ID that is not exactly one real model."""


def resolve_model(requested: str, available: Sequence[str]) -> Resolution:
    """Match a requested ID against the account's catalogue. Never guesses.

    Four outcomes, and three of them are refusals:

    * **EXACT** — the ID is in the catalogue verbatim. The only usable result.
    * **AMBIGUOUS** — the ID is the family of two or more real models. This is
      the ``gpt-5.6`` case. Refused, with the siblings named, because they
      differ in price and capability and picking one is the user's call.
    * **MISSING** — nothing matches. Nearest names offered as a hint, never as
      a substitution.
    * **UNVERIFIED** — no catalogue was available. Not a pass. An agent that
      cannot check should say so, not proceed as though it had.
    """
    req = (requested or "").strip()
    if not req:
        return Resolution(req, MISSING, message="no model ID was requested")
    if not available:
        return Resolution(req, UNVERIFIED, message=(
            f"could not verify {req!r}: no model catalogue was available. "
            "Wire a ModelCatalogue, or list the models by hand — the agent is "
            "running on an unchecked ID."))

    if req in available:
        return Resolution(req, EXACT, resolved=req,
                          message=f"{req} is available")

    # The family case: the request is a real prefix of several real models.
    variants = [m for m in available if m.startswith(req + "-")]
    if len(variants) == 1:
        return Resolution(req, AMBIGUOUS, candidates=tuple(variants), message=(
            f"{req!r} does not exist; the account has {variants[0]!r}. "
            f"Name it explicitly — a near-match is not a match."))
    if variants:
        listed = ", ".join(variants)
        return Resolution(req, AMBIGUOUS, candidates=tuple(variants), message=(
            f"{req!r} does not exist. It is the family name for {len(variants)} "
            f"models the account does have: {listed}. They differ in price, "
            f"latency and capability, so pick one deliberately — this will not "
            f"choose for you."))

    near = tuple(difflib.get_close_matches(req, list(available), n=4, cutoff=0.5))
    same_family = tuple(m for m in available
                        if family_of(m) == family_of(req) and m not in near)
    hints = near + same_family
    hint_text = (" Closest available: " + ", ".join(hints[:5])) if hints else ""
    return Resolution(req, MISSING, candidates=hints, message=(
        f"{req!r} is not in this account's catalogue of {len(available)} "
        f"models.{hint_text}"))


# ---------------------------------------------------------------------------
# 3. Everything else that must be true before turn one
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Requirement:
    """What the agent needs from the model, declared where it is decided."""

    tools: bool = True               # the agent dispatches tools
    streaming: bool = False
    thinking: bool = False           # extended reasoning
    vision: bool = False
    min_context_tokens: int = 0
    min_output_tokens: int = 0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True


@dataclass
class Preflight:
    """The report. Every check names what to do about it."""

    provider: str
    checks: list[Check] = field(default_factory=list)
    resolution: Resolution | None = None

    def add(self, name: str, ok: bool, detail: str = "", fatal: bool = True) -> None:
        self.checks.append(Check(name, ok, detail, fatal))

    @property
    def ok(self) -> bool:
        return not any(c.fatal and not c.ok for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def render(self) -> str:
        lines = [f"preflight: {self.provider}"]
        for c in self.checks:
            mark = "ok  " if c.ok else ("FAIL" if c.fatal else "warn")
            lines.append(f"  [{mark}] {c.name}" + (f" — {c.detail}" if c.detail else ""))
        lines.append("  => " + ("ready" if self.ok else "NOT ready"))
        return "\n".join(lines)


def check_key(provider: str, env: dict[str, str] | None = None) -> Check:
    """Presence only. The value is never read into a message or a log."""
    env = os.environ if env is None else env
    spec = CATALOGUE.get(provider)
    if spec is None:
        return Check("api key", False, f"unknown provider {provider!r}")
    name = spec["key_env"]
    raw = env.get(name, "")
    if not raw.strip():
        return Check("api key", False, f"{name} is unset or empty")
    return Check("api key", True, f"{name} is set")


def preflight(provider: str,
              requested_model: str,
              available: Sequence[str],
              requirement: Requirement | None = None,
              *,
              limits: dict[str, Any] | None = None,
              env: dict[str, str] | None = None,
              ping_ok: bool | None = None,
              ping_detail: str = "") -> Preflight:
    """Everything checkable before the first real call, as one report.

    ``limits`` is whatever the provider publishes about the resolved model
    (context window, max output, modalities). Absent, capability checks warn
    rather than fail — an unpublished limit is unknown, not violated.
    """
    requirement = requirement or Requirement()
    rep = Preflight(provider)

    key = check_key(provider, env)
    rep.checks.append(key)

    res = resolve_model(requested_model, available)
    rep.resolution = res
    rep.add("model id", res.ok, res.message,
            fatal=res.status != UNVERIFIED)
    if res.status == UNVERIFIED:
        rep.checks[-1].fatal = False

    if limits:
        ctx = limits.get("context_window") or limits.get("max_input_tokens")
        if requirement.min_context_tokens and isinstance(ctx, int):
            rep.add("context window", ctx >= requirement.min_context_tokens,
                    f"{ctx} available, {requirement.min_context_tokens} needed")
        out = limits.get("max_output_tokens")
        if requirement.min_output_tokens and isinstance(out, int):
            rep.add("max output", out >= requirement.min_output_tokens,
                    f"{out} available, {requirement.min_output_tokens} needed")
        for flag, key_name in (("tools", "supports_tools"),
                               ("streaming", "supports_streaming"),
                               ("thinking", "supports_thinking"),
                               ("vision", "supports_vision")):
            if getattr(requirement, flag) and key_name in limits:
                rep.add(flag, bool(limits[key_name]),
                        "published as unsupported" if not limits[key_name] else "")
    elif any((requirement.min_context_tokens, requirement.min_output_tokens)):
        rep.add("capability limits", True,
                "provider published none; unchecked", fatal=False)

    if ping_ok is not None:
        rep.add("live call", ping_ok, ping_detail or
                ("a one-token call succeeded" if ping_ok else "a one-token call failed"))
    else:
        rep.add("live call", True, "not attempted", fatal=False)
    return rep


# ---------------------------------------------------------------------------
# 4. The seam
# ---------------------------------------------------------------------------

class ModelCatalogue(Protocol):
    """What a host implements so preflight can be automatic."""

    def list_models(self, provider: str) -> list[str]: ...

    def ping(self, provider: str, model_id: str) -> tuple[bool, str]:
        """One minimal call. Returns (ok, detail)."""


@dataclass
class UnconfiguredCatalog:
    """The default: refuses, and prints the request to make by hand."""

    def list_models(self, provider: str) -> list[str]:
        raise UnconfiguredCatalogue(curl_for(provider))

    def ping(self, provider: str, model_id: str) -> tuple[bool, str]:
        return (False, "no ModelCatalogue wired in; live call not attempted")


class UnconfiguredCatalogue(RuntimeError):
    """Raised by :class:`UnconfiguredCatalog`; the message is the instructions."""


def curl_for(provider: str) -> str:
    """The exact command that lists this provider's models."""
    spec = CATALOGUE.get(provider)
    if spec is None:
        return f"unknown provider {provider!r}; known: {', '.join(sorted(CATALOGUE))}"
    key_env = spec["key_env"]
    parts = [f"curl -sS {spec['url']}",
             f"  -H '{spec['auth'].format(key='$' + key_env)}'"]
    if spec["extra"]:
        parts.append(f"  -H '{spec['extra']}'")
    return " \\\n".join(parts) + "  | python3 -m json.tool"
