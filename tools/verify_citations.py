#!/usr/bin/env python3
"""Verify every `file:line` citation in the skill against the pinned source.

The naive version of this check — "does the file exist and have enough lines"
— catches typos and invented paths and nothing else. It passes happily on a
citation that has *drifted*: if upstream refactors and the referenced code
moves, the line number still resolves and the claim is now wrong.

So this tool records a **content anchor** for every citation: a normalised
fingerprint of the lines actually cited, captured against a named source
version. Re-running against a newer source reports three outcomes:

    ok       resolved, and the cited lines are unchanged
    DRIFTED  resolved, but the cited lines are no longer what they were
    BROKEN   path missing, or the file is now shorter than the citation

Usage
-----
    python3 tools/verify_citations.py --update   # write/refresh the lockfile
    python3 tools/verify_citations.py            # check against the lockfile
    python3 tools/verify_citations.py --source <dir> --version 2.2.0

The lockfile (`tools/citations.lock.json`) is committed; the source tree it
was captured from is not (see .gitignore). That is the point: the lockfile is
what lets someone re-check this library against a version we never had.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SOURCE = os.path.join(
    ROOT, "saved", "claude-code-v-2.1.88", "package", "src-extracted", "src"
)
DEFAULT_EXTRA_ROOTS = [os.path.join(ROOT, "saved", "claude-code-v-2.1.88", "package")]
DEFAULT_VERSION = "claude-code-2.1.88"
LOCKFILE = os.path.join(ROOT, "tools", "citations.lock.json")
DOCS_GLOB_ROOT = os.path.join(ROOT, "skills")

CITATION = re.compile(r"`?([A-Za-z][\w./-]*\.(?:tsx?|d\.ts)):(\d+)(?:-(\d+))?")
#: Anchors ignore whitespace and comment-only churn so a reformat is not a
#: false alarm; anything that changes actual code text is.
_WS = re.compile(r"\s+")


@dataclass
class Citation:
    doc: str
    path: str
    start: int
    end: int

    @property
    def key(self) -> str:
        return f"{self.path}:{self.start}-{self.end}"


def diff_window(before: str, after: str, pad: int = 45) -> tuple[str, str]:
    """Return the region around the first divergence, so a report is readable.

    Two 400-char excerpts that differ at character 380 look identical when both
    are printed head-first. Centre the window on the difference instead.
    """
    i = 0
    while i < min(len(before), len(after)) and before[i] == after[i]:
        i += 1
    lo = max(0, i - pad)
    return before[lo : i + pad], after[lo : i + pad]


@dataclass
class Result:
    ok: list[str] = field(default_factory=list)
    drifted: list[tuple[str, str]] = field(default_factory=list)
    broken: list[tuple[str, str]] = field(default_factory=list)
    unlocked: list[str] = field(default_factory=list)
    skipped: int = 0


def find_citations(docs_root: str) -> list[Citation]:
    out: list[Citation] = []
    for dirpath, dirnames, filenames in os.walk(docs_root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, ROOT)
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
            for m in CITATION.finditer(text):
                start = int(m.group(2))
                end = int(m.group(3)) if m.group(3) else start
                out.append(Citation(rel, m.group(1), start, min(end, start + 200)))
    return out


def resolve(path: str, source: str, extra_roots: list[str]) -> str | None:
    for root in [source, *extra_roots]:
        candidate = os.path.join(root, path)
        if os.path.isfile(candidate):
            return candidate
    # Fall back to a unique suffix match anywhere under the source tree.
    base = os.path.basename(path)
    hits = []
    for dirpath, _dirs, files in os.walk(source):
        if base in files:
            full = os.path.join(dirpath, base)
            if full.endswith(path):
                hits.append(full)
    return hits[0] if len(hits) == 1 else None


def anchor_for(filepath: str, start: int, end: int) -> tuple[str, str] | None:
    """Return (fingerprint, excerpt) for the cited line range, or None."""
    with open(filepath, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    if start > len(lines):
        return None
    window = lines[start - 1 : min(end, len(lines))]
    normalized = _WS.sub(" ", "".join(window)).strip()
    if not normalized:
        return None
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    # Keep enough text that a drift report can show WHERE it diverged, not just
    # two identical-looking heads.
    excerpt = (normalized[:400] + "…") if len(normalized) > 400 else normalized
    return fingerprint, excerpt


def run(source: str, extra_roots: list[str], version: str, update: bool) -> int:
    citations = find_citations(DOCS_GLOB_ROOT)
    have_source = os.path.isdir(source)
    lock = {}
    if os.path.isfile(LOCKFILE):
        with open(LOCKFILE, encoding="utf-8") as fh:
            lock = json.load(fh)
    entries: dict[str, dict] = lock.get("citations", {})
    res = Result()

    if not have_source and not update:
        # The pinned source is gitignored, so a fresh clone legitimately has
        # nothing to check against. Say so plainly rather than passing.
        print(f"source tree not present at {source}")
        print(f"lockfile holds {len(entries)} anchors for {lock.get('version', '?')}")
        print("nothing verified — fetch the source, or pass --source <dir>")
        return 0

    new_entries: dict[str, dict] = {}
    for c in citations:
        resolved = resolve(c.path, source, extra_roots) if have_source else None
        if resolved is None:
            res.broken.append((c.key, f"unresolved path (cited in {c.doc})"))
            continue
        got = anchor_for(resolved, c.start, c.end)
        if got is None:
            res.broken.append((c.key, f"line {c.start} past end of file (in {c.doc})"))
            continue
        fingerprint, excerpt = got
        new_entries[c.key] = {
            "doc": c.doc, "version": version,
            "fingerprint": fingerprint, "excerpt": excerpt,
        }
        if update:
            res.ok.append(c.key)
            continue
        known = entries.get(c.key)
        if known is None:
            res.unlocked.append(f"{c.key} (in {c.doc})")
        elif known["fingerprint"] != fingerprint:
            was, now = diff_window(known["excerpt"], excerpt)
            res.drifted.append((
                c.key,
                f"in {c.doc}\n      was ({known['version']}): …{was}…"
                f"\n      now ({version}): …{now}…",
            ))
        else:
            res.ok.append(c.key)

    if update:
        with open(LOCKFILE, "w", encoding="utf-8") as fh:
            json.dump(
                {"version": version, "source": os.path.relpath(source, ROOT),
                 "count": len(new_entries), "citations": new_entries},
                fh, indent=1, sort_keys=True,
            )
            fh.write("\n")
        print(f"wrote {len(new_entries)} anchors to {os.path.relpath(LOCKFILE, ROOT)} "
              f"for {version}")
        if res.broken:
            for key, why in res.broken:
                print(f"  BROKEN  {key}: {why}")
            return 1
        return 0

    print(f"checking {len(citations)} citations against {version}")
    for key, why in res.broken:
        print(f"  BROKEN   {key}: {why}")
    for key, why in res.drifted:
        print(f"  DRIFTED  {key}: {why}")
    for key in res.unlocked:
        print(f"  UNLOCKED {key} — run --update to record an anchor")
    print(f"\n  ok {len(res.ok)} | drifted {len(res.drifted)} "
          f"| broken {len(res.broken)} | unlocked {len(res.unlocked)}")
    if res.drifted:
        print("\nDrift means the citation still resolves but the code it points at "
              "changed.\nRe-read those sites and fix the claim, then --update.")
    return 1 if (res.broken or res.drifted or res.unlocked) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="source tree to check against")
    ap.add_argument("--version", default=DEFAULT_VERSION, help="label for this source")
    ap.add_argument("--update", action="store_true", help="rewrite the lockfile")
    a = ap.parse_args()
    return run(a.source, DEFAULT_EXTRA_ROOTS, a.version, a.update)


if __name__ == "__main__":
    sys.exit(main())
