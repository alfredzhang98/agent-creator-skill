"""Verification when there is no compiler: baseline-diff, advisory signals.

Distilled from Claude Code 2.1.88 ``src/services/diagnosticTracking.ts`` and
``src/services/lsp/passiveFeedback.ts``; the signal vocabulary matches
Articraft's ``CompileSignal`` (reference 03).

There are three tiers of verification, and most agents wrongly assume they are
in tier 1 or tier 3:

1. **Gating** — a mechanical check decides whether the run may finish
   (Articraft: compile the model, or you did not succeed). Best when available.
2. **Advisory** — a mechanical check runs continuously and reports, but does
   not block. *This module.* It is what you build when the domain has real
   checkers (type checker, linter, test suite, schema validator) but no single
   oracle for "done".
3. **Human** — a person decides (reference 13).

The tier-2 mistake everyone makes is reporting **all** current problems. Run a
type checker on a real repository and it prints 400 pre-existing warnings; the
model then either fixes unrelated code or learns to ignore the channel. Both
are worse than silence.

The fix is a **baseline diff scoped to attribution**: snapshot the checker's
output for a file *before* the agent touches it, and afterwards report only
what is new. The question becomes "did *your* edit break something", which is
the only one the agent can act on.
"""
from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

#: Ordered most to least severe. Shared with the loop, the renderer and the
#: persistence layer so severity never gets re-invented per consumer.
SEVERITIES = ("error", "warning", "info", "hint")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

#: Cap on one injection. Past this, the model stops reading and starts guessing.
MAX_REPORT_CHARS = 4_000


def normalize_path(path: str) -> str:
    """Canonical identity for a file.

    Callers mix absolute paths (what an edit tool holds) with repo-relative
    ones (what mypy and ruff print) on day one. Comparing raw strings makes a
    mismatch look like "someone else's file" and silently discards every
    finding — the failure is total and invisible, so normalise on both the
    write and the read path.
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


@dataclass(frozen=True)
class Signal:
    """One finding. Comparable by value so baseline diffing works."""

    path: str
    message: str
    severity: str = "error"
    line: int | None = None
    column: int | None = None
    source: str = ""        # which checker produced it: "mypy", "eslint", …
    code: str = ""          # the checker's own rule id

    def __post_init__(self) -> None:
        # Normalise on construction so a checker adapter emitting LSP's
        # "Error" or an integer severity cannot crash the diff, and so a
        # capitalised severity cannot silently fail to block.
        object.__setattr__(self, "severity", normalize_severity(self.severity))

    def key(self) -> tuple:
        """Identity for diffing.

        EXCLUDES the line number: an edit above a pre-existing error shifts
        its line and would otherwise resurface it as "new". Multiplicity is
        preserved separately (the baseline is a multiset), because dropping
        both loses genuinely new occurrences of a repeated message.
        """
        return (normalize_path(self.path), self.severity, self.source,
                self.code, self.message)

    def render(self) -> str:
        loc = f":{self.line}" if self.line is not None else ""
        origin = f" [{self.source}{':' + self.code if self.code else ''}]" if self.source else ""
        return f"{self.severity.upper()} {self.path}{loc}{origin}: {self.message}"


#: Spellings real checkers emit, mapped onto the shared vocabulary.
_SEVERITY_ALIASES = {
    "err": "error", "fatal": "error", "critical": "error", "high": "error",
    "warn": "warning", "medium": "warning",
    "information": "info", "informational": "info", "note": "info", "low": "info",
    "style": "hint", "suggestion": "hint", "convention": "hint", "refactor": "hint",
}


def normalize_severity(raw: Any, default: str = "error") -> str:
    """Map a checker's own severity onto the shared vocabulary.

    Unknown values become the default rather than being dropped: a finding you
    cannot classify is still a finding, and discarding it is how a verifier
    stops catching the thing it was built for. Note the bool guard — `bool` is
    a subclass of `int`, so `True` would otherwise read as LSP severity 1.
    """
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in _RANK:
            return low
        if low in _SEVERITY_ALIASES:
            return _SEVERITY_ALIASES[low]
        return default
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):                     # LSP: 1=Error … 4=Hint
        return SEVERITIES[raw - 1] if 1 <= raw <= len(SEVERITIES) else default
    return default


@dataclass
class CheckResult:
    """What a checker adapter returns.

    ``checked`` is not optional bookkeeping — it is the difference between
    "this file is clean now" and "the checker never ran". Without it, a
    crashed or missing checker returns no findings, the baseline is zeroed,
    and every pre-existing problem is reported as new on the following run.
    """

    checked: frozenset[str]
    signals: tuple[Signal, ...] = ()

    @classmethod
    def clean(cls, paths: Iterable[str]) -> "CheckResult":
        return cls(frozenset(normalize_path(p) for p in paths))

    @classmethod
    def of(cls, paths: Iterable[str], signals: Iterable[Signal]) -> "CheckResult":
        return cls(frozenset(normalize_path(p) for p in paths), tuple(signals))

    @classmethod
    def unavailable(cls) -> "CheckResult":
        """The checker could not run. Nothing is learned; nothing is forgotten."""
        return cls(frozenset())


class BaselineVerifier:
    """Tracks per-file checker output so only *new* findings are reported.

    Usage, wired into the tool pipeline::

        verifier.before_edit(path, check_one(path))   # BEFORE the write
        ...                                            # the edit happens
        report = verifier.new_findings(run_checker)    # after the tool batch
    """

    def __init__(self, min_severity: str = "warning"):
        #: path -> multiset of finding keys. A Counter, not a set: two
        #: occurrences of the same message in one file are two findings, and
        #: collapsing them hides the second one when it appears.
        self._baseline: dict[str, Counter] = {}
        self._min_rank = _RANK[normalize_severity(min_severity, "warning")]

    def before_edit(self, path: str, findings: Iterable[Signal]) -> None:
        """Snapshot a file's current findings. Idempotent within a turn.

        *findings* is required, deliberately. A default of "no findings"
        records a clean baseline for a file that may have four hundred
        pre-existing warnings, and then reports all four hundred as caused by
        this edit — precisely the failure this class exists to prevent. Pass
        an explicit empty iterable when the file really is clean.
        """
        key = normalize_path(path)
        if key not in self._baseline:
            self._baseline[key] = Counter(f.key() for f in findings)

    def forget(self, path: str) -> None:
        """Stop tracking a file (deleted, renamed away, or out of scope).

        Without eviction the tracked set grows for the whole session and the
        checker is re-run over every file ever touched.
        """
        self._baseline.pop(normalize_path(path), None)

    def rename(self, old: str, new: str, findings: Iterable[Signal]) -> None:
        """Move a baseline with the file. A rename otherwise drops every
        finding in the new path and leaks the old one forever."""
        self.forget(old)
        self.before_edit(new, findings)

    def tracked(self) -> list[str]:
        return sorted(self._baseline)

    def new_findings(
        self, check: Callable[[Sequence[str]], CheckResult]
    ) -> list[Signal]:
        """Run *check* over touched files and return only unseen findings.

        The baseline advances only for paths the checker says it actually
        checked, so a partial or failed run cannot erase what we knew.
        """
        if not self._baseline:
            return []
        paths = self.tracked()
        try:
            result = check(paths)
        except Exception:  # noqa: BLE001 - a broken checker must not stop the loop
            return []
        if not isinstance(result, CheckResult):
            raise TypeError(
                "check() must return a CheckResult so the verifier can tell "
                "'clean' from 'did not run'. Use CheckResult.of(paths, signals), "
                ".clean(paths), or .unavailable()."
            )

        current: dict[str, Counter] = {p: Counter() for p in result.checked
                                       if p in self._baseline}
        fresh: list[Signal] = []
        for signal in result.signals:
            key_path = normalize_path(signal.path)
            # Not attributable to this agent's edits, so not actionable by it.
            if key_path not in self._baseline:
                continue
            k = signal.key()
            current.setdefault(key_path, Counter())[k] += 1
            # Multiset comparison: the Nth occurrence is new if the baseline
            # had fewer than N.
            if current[key_path][k] <= self._baseline[key_path][k]:
                continue
            if _RANK[signal.severity] > self._min_rank:
                continue
            fresh.append(signal)

        # Advance only what was actually checked.
        for path, counts in current.items():
            self._baseline[path] = counts
        return sorted(fresh, key=lambda s: (_RANK[s.severity], s.path, s.line or 0))

    def reset(self) -> None:
        """Clear all tracking. Call at the start of each user turn."""
        self._baseline.clear()


@dataclass
class Report:
    signals: list[Signal] = field(default_factory=list)

    @property
    def blocking(self) -> list[Signal]:
        return [s for s in self.signals if s.severity == "error"]

    def render(self, max_chars: int = MAX_REPORT_CHARS) -> str:
        """Render for the model: highest severity first, truncation stated.

        Stops at the first line that does not fit rather than skipping it and
        printing a later, shorter one — otherwise the model sees a scrambled
        subset while the header implies these are the important ones. The
        footer is reserved up front so the result never exceeds *max_chars*.
        """
        if not self.signals:
            return ""
        head = (
            "New problems appeared in files you just changed. These were not "
            "present before your edit:"
        )
        ordered = sorted(
            self.signals, key=lambda s: (_RANK[s.severity], s.path, s.line or 0)
        )
        footer_budget = 70
        lines, used, shown = [], len(head), 0
        for sig in ordered:
            line = sig.render()
            if used + len(line) + 1 > max_chars - footer_budget:
                break
            lines.append(line)
            used += len(line) + 1
            shown += 1
        body = "\n".join(lines)
        dropped = len(ordered) - shown
        if dropped:
            body += f"\n({dropped} more not shown — fix these first, then re-check.)"
        return f"{head}\n{body}"


def should_report(report: Report, agent_can_act: bool) -> bool:
    """Inject only when the agent has some way to act on it.

    Claude Code gates diagnostics on the agent having a shell tool at all —
    "diagnostics are only useful if the agent has the Bash tool to act on them"
    (`utils/attachments.ts:2857-2862`). Feedback an agent cannot act on is pure
    context cost, and it teaches the model that this channel is noise.
    """
    return bool(report.signals) and agent_can_act
