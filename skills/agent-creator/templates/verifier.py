"""Structured verification signals, LLM rendering, and revision-keyed caching.

Distills ``articraft.compiler.feedback`` (typed signal vocabulary,
classification, bundle building, primary-issue selection, response rules),
``articraft.compiler.result``, and the verify-freshness bookkeeping the
harness keeps in ``articraft.agent.harness``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

SEVERITIES = ("failure", "warning", "note")   # only failures block


@dataclass(frozen=True)
class Signal:
    """One finding: machine-stable slugs + human text, fully serializable."""

    severity: str            # "failure" | "warning" | "note"
    kind: str                # e.g. "runtime", "structural", "authored_qc"
    code: str                # stable slug for tests/metrics
    summary: str
    details: str = ""
    blocking: bool = False
    source: str = "checker"  # "checker" | "authored_tests" | "harness"
    group: str = "qc"        # "build" | "qc" | "design" | "hygiene"

    def dedupe_key(self) -> str:
        raw = "|".join((self.severity, self.kind, self.code, self.summary, self.details))
        return hashlib.sha1(raw.encode()).hexdigest()


# Lower number = more fundamental; coach the model to fix root causes first.
KIND_PRIORITY: dict[str, int] = {"runtime": 0, "structural": 1, "global_qc": 2,
                                 "stale_contract": 3, "authored_qc": 4, "generic": 7}

# 2-6 imperative bullets per kind; rendered under <response_rules>.
RULES_BY_KIND: dict[str, list[str]] = {
    "runtime": ["Read the traceback and fix the crash before any design change."],
    "structural": ["Restore the required structure first; re-verify before tuning."],
    "generic": ["Diagnose with a probe/read before editing again."],
}

# Raw checker text -> typed signal; most specific first, nothing is dropped.
CLASSIFIERS: list[tuple[Callable[[str], bool], Callable[[str], Signal]]] = []


def classify(text: str) -> Signal:
    """Map raw output to a typed signal; unknown text becomes a generic warning."""
    for matches, build in CLASSIFIERS:
        if matches(text):
            return build(text)
    head = text.strip().splitlines()[0][:200] if text.strip() else "unknown finding"
    return Signal("warning", "generic", "unclassified", head,
                  details=text.strip()[:2000])


@dataclass
class Bundle:
    """All signals from one verification attempt, deduped and ordered."""

    status: str                        # "ok" | "failure"
    signals: tuple[Signal, ...] = ()

    def _sev(self, severity: str) -> list[Signal]:
        return [s for s in self.signals if s.severity == severity]

    @property
    def failures(self) -> list[Signal]:
        return self._sev("failure")

    def primary_issue(self) -> Signal | None:
        """The single most fundamental failure; drives the response rules."""
        if not self.failures:
            return None
        return min(self.failures,
                   key=lambda s: (KIND_PRIORITY.get(s.kind, 90), s.kind, s.summary))

    def signature(self) -> str:
        """Stable digest for repeated-failure detection across attempts."""
        keys = ",".join(sorted(s.dedupe_key() for s in self.signals))
        return hashlib.sha1(f"{self.status}|{keys}".encode()).hexdigest()

    def render(self, repeated: bool = False, streak: int = 0) -> str:
        """The feedback block the LLM sees: summary, findings, next steps."""
        fails, warns, notes = self.failures, self._sev("warning"), self._sev("note")
        primary = self.primary_issue()
        lines = ["<signals>",
                 f"<summary>status={self.status} failures={len(fails)} "
                 f"warnings={len(warns)} notes={len(notes)}"]
        if primary:
            lines.append(f"Primary issue: {primary.summary}")
        if repeated:
            lines.append("This failure matches the previous attempt.")
        if streak >= 3:
            lines.append(f"This is failure {streak} in a row.")
        lines.append("</summary>")
        for title, group in (("failures", fails), ("warnings", warns), ("notes", notes)):
            if group:
                lines.append(f"<{title}>")
                for s in group:
                    lines.append(f"- {s.severity.upper()} [{s.kind}] {s.summary}")
                    if s.details:
                        lines.append("  " + s.details.replace("\n", "\n  "))
                lines.append(f"</{title}>")
        lines.append("<response_rules>")
        rules = RULES_BY_KIND.get(primary.kind, RULES_BY_KIND["generic"]) \
            if primary else ["Warnings are design evidence; address or justify them."]
        lines.extend(f"- {r}" for r in rules)
        if repeated:
            lines.append("- Probe/diagnose instead of another blind tweak.")
        lines.append("</response_rules>")
        lines.append("</signals>")
        return "\n".join(lines)


class Verifier:
    """Revision-keyed cache around an expensive check function.

    The loop calls mark_mutated() after every successful mutating tool, and
    latest_is_fresh() gates finishing: a text-only answer only succeeds when
    the cached PASSING bundle belongs to the current edit revision.
    """

    verify_tool_name = "verify"

    def __init__(self, check: Callable[[], Bundle]) -> None:
        self._check = check              # run inside sandbox_runner for isolation
        self.edit_revision = 0           # bumped by every successful mutating tool
        self._cached: tuple[int, Bundle] | None = None   # (revision, passing bundle)
        self.attempt_count = 0
        self.failure_streak = 0
        self.last_failure_sig: str | None = None

    def mark_mutated(self) -> None:
        self.edit_revision += 1          # any edit invalidates the cached pass

    def latest_is_fresh(self) -> bool:
        return self._cached is not None and self._cached[0] == self.edit_revision

    def run_cached(self) -> str:
        """Run (or replay) verification; returns the rendered bundle text."""
        if self.latest_is_fresh():
            return ("A fresh verification result already exists; treat it as "
                    "authoritative.\n" + self._cached[1].render())
        self.attempt_count += 1
        bundle = self._check()
        if bundle.failures:
            sig = bundle.signature()
            repeated = sig == self.last_failure_sig
            self.failure_streak += 1
            self.last_failure_sig = sig
            return bundle.render(repeated=repeated, streak=self.failure_streak)
        self.failure_streak, self.last_failure_sig = 0, None
        self._cached = (self.edit_revision, bundle)   # cache ONLY passing results
        return bundle.render()
