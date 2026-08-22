"""Prompts as code: a build matrix, memoised sections, and a cache boundary.

Adapted from Articraft `agent/prompts/{spec,compile}.py` (Apache-2.0) and
Claude Code 2.1.88 `src/constants/systemPromptSections.ts`.

A system prompt that is one long string has three problems you discover in
this order: you cannot tell which paragraph caused a behaviour change, you
cannot vary it per provider without forking it, and you cannot stop it
invalidating the prompt cache. Treating it as a compiled artifact fixes all
three, and costs about a hundred lines.

Two ideas do the work:

* **A build matrix.** Variants are lists of shared sections. Adding a provider
  is a row, not a fork, and the diff between two variants is legible.
* **A declared cache boundary.** Everything before it can be cached across
  sessions — often across organisations. Everything after is volatile. Making
  a section volatile is possible but must be justified *at the call site*,
  because the cost lands on every future turn and is invisible locally.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

#: Separates the cross-session cacheable prefix from the volatile tail.
#: Everything above it must be byte-identical between runs, or the cache is
#: not a cache. Anything user-, time- or session-specific goes below.
DYNAMIC_BOUNDARY = "__PROMPT_DYNAMIC_BOUNDARY__"


@dataclass(frozen=True)
class Section:
    """One named piece of prompt, and whether it may break the cache."""

    name: str
    #: Called at build time. Return None to omit the section entirely.
    compute: Callable[[], str | None]
    #: True only via `volatile()`, which demands a written reason.
    cache_break: bool = False
    reason: str = ""


def section(name: str, compute: Callable[[], str | None]) -> Section:
    """A stable section: computed once, cached until the session resets."""
    return Section(name, compute)


def volatile(name: str, compute: Callable[[], str | None], reason: str) -> Section:
    """A section that recomputes every turn. **This breaks the prompt cache.**

    The *reason* is required and is retained on the object so a later reader
    can audit it. Cache-breaking is cheap to add and expensive forever, and
    the expense is invisible in local testing — a required argument is the
    cheapest possible speed bump.
    """
    if not reason.strip():
        raise ValueError(
            f"volatile section {name!r} needs a reason: it recomputes every "
            "turn and invalidates the cached prefix from that point on"
        )
    return Section(name, compute, cache_break=True, reason=reason)


@dataclass
class SectionCache:
    """Memoises stable sections. Clear on /clear, /compact, or a new session."""

    values: dict[str, str | None] = field(default_factory=dict)

    def resolve(self, sections: Sequence[Section]) -> list[str]:
        out: list[str] = []
        for s in sections:
            if s.cache_break or s.name not in self.values:
                self.values[s.name] = s.compute()
            value = self.values[s.name]
            if value:
                out.append(value.rstrip("\n"))
        return out

    def clear(self) -> None:
        self.values.clear()


# --------------------------------------------------------------------------
# The build matrix
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Variant:
    """One compiled prompt: a named list of section files.

    Sections are shared by reference, so two providers that differ only in
    their tool contract share every other paragraph — and the difference
    between them is readable as a list rather than as a diff of two 10 KB
    strings.
    """

    name: str
    sections: tuple[str, ...]
    output: str
    description: str = ""


def compile_variant(variant: Variant, sections_dir: str) -> str:
    """Concatenate the variant's sections. Deterministic, byte for byte."""
    parts: list[str] = []
    for rel in variant.sections:
        path = os.path.join(sections_dir, rel)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read().rstrip("\n")
        if text:
            parts.append(text)
    return "\n\n".join(parts) + "\n"


def write_variant(variant: Variant, sections_dir: str, out_dir: str) -> bool:
    """Write if changed. Returns whether anything was written."""
    compiled = compile_variant(variant, sections_dir)
    path = os.path.join(out_dir, variant.output)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read() == compiled:
                return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(compiled)
    return True


def stale_variants(variants: Iterable[Variant], sections_dir: str,
                   out_dir: str) -> list[Variant]:
    """Which compiled artifacts no longer match their sources.

    Wire this into CI. A generated prompt that drifts from its sources is a
    prompt nobody can reason about: the file you read is not the file that
    shipped.
    """
    stale = []
    for v in variants:
        path = os.path.join(out_dir, v.output)
        if not os.path.isfile(path):
            stale.append(v)
            continue
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read() != compile_variant(v, sections_dir):
                stale.append(v)
    return stale


def distinct_artifacts(variants: Sequence[Variant], sections_dir: str) -> dict[str, list[str]]:
    """Group variants by compiled content.

    Worth running once: variants that differ in name but not in bytes are
    usually a copy-paste that has stopped meaning anything, and each one still
    costs a build step and a reader's attention.
    """
    groups: dict[str, list[str]] = {}
    for v in variants:
        digest = hashlib.sha256(compile_variant(v, sections_dir).encode()).hexdigest()[:12]
        groups.setdefault(digest, []).append(v.name)
    return groups


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

@dataclass
class Prompt:
    """A built system prompt, split at the cache boundary."""

    stable: str
    volatile: str

    def blocks(self) -> list[dict[str, object]]:
        """Render for an API that supports per-block cache control."""
        out: list[dict[str, object]] = []
        if self.stable:
            out.append({"type": "text", "text": self.stable,
                        "cache_control": {"type": "ephemeral"}})
        if self.volatile:
            out.append({"type": "text", "text": self.volatile})
        return out

    def text(self) -> str:
        return "\n\n".join(p for p in (self.stable, self.volatile) if p)

    @property
    def cache_key(self) -> str:
        """Content-address the STABLE half only.

        Keying on the whole prompt means the volatile tail changes the key
        every turn, which is the same as having no cache. Keying on the stable
        half means any real prompt change still invalidates correctly.
        """
        return "p1:" + hashlib.sha256(self.stable.encode("utf-8")).hexdigest()[:32]


def build(sections: Sequence[Section], cache: SectionCache | None = None) -> Prompt:
    """Resolve sections and split them at the boundary marker.

    A section named `DYNAMIC_BOUNDARY` (or any section emitting exactly that
    marker) separates the halves. Sections declared volatile are moved below
    the boundary automatically, because leaving one above it silently defeats
    the whole mechanism.
    """
    cache = cache or SectionCache()
    stable: list[str] = []
    tail: list[str] = []
    below = False
    for s in sections:
        if s.name == DYNAMIC_BOUNDARY:
            below = True
            continue
        value = cache.resolve([s])
        if not value:
            continue
        text = value[0]
        if text.strip() == DYNAMIC_BOUNDARY:
            below = True
            continue
        (tail if below or s.cache_break else stable).append(text)
    return Prompt("\n\n".join(stable), "\n\n".join(tail))


def audit(sections: Sequence[Section]) -> list[str]:
    """Report every cache-breaking section and its stated reason.

    Run it in a test. The number should be small and each entry should be
    defensible out loud; a list that grows without anyone noticing is how a
    cached prefix quietly stops being cached.
    """
    return [f"{s.name}: {s.reason}" for s in sections if s.cache_break]
