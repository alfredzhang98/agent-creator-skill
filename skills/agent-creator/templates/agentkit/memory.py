"""Memory: what an agent should remember across sessions, and what it must not.

Distilled from Claude Code 2.1.88 ``src/memdir/`` (`memoryTypes.ts`,
`memoryScan.ts`, `findRelevantMemories.ts`, `memoryAge.ts`, `memdir.ts`).

The hard part of memory is not storage. It is the two questions storage cannot
answer:

**What is worth saving?** Claude Code's answer is a single rule, stated at the
top of the taxonomy: memories capture *context not derivable from the current
project state* — "code patterns, architecture, git history, and file structure
are derivable (via grep/git/CLAUDE.md) and should NOT be saved"
(`memdir/memoryTypes.ts:1-12`). Everything an agent can rediscover in ten
seconds is noise in a memory store; what it cannot rediscover at any price is
what happened in a conversation nobody wrote down.

**What is worth recalling?** Not everything relevant — everything relevant
*that changes what you would do*. Recall runs the same three-level ladder as
skills (reference 11): a manifest of headers is always cheap, a cheap model
picks at most five, and only those are read in full.

The third problem is subtler and this module treats it as first-class: a
memory is a **point-in-time observation, not live state**. A 47-day-old note
citing `foo.py:112` is more dangerous than no note, because the citation makes
a stale claim sound authoritative.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

#: Closed taxonomy. Each type answers a different question about the future.
MEMORY_TYPES = ("user", "feedback", "project", "reference")

ENTRYPOINT_NAME = "MEMORY.md"
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000
#: Recall budget per turn. Small on purpose — see `select`.
MAX_RECALLED = 5
#: Cumulative per-session injection cap; past this, stop recalling entirely.
MAX_SESSION_BYTES = 60 * 1024
MAX_MEMORY_BYTES = 4096

DAY_MS = 86_400_000


@dataclass(frozen=True)
class MemoryHeader:
    """What the selector sees. Never the body — that is the whole point."""

    path: str
    name: str
    description: str
    type: str | None
    mtime_ms: float

    @property
    def filename(self) -> str:
        """What the selector is asked to return, and what `select` keys on.

        The prompt and the manifest must agree on this or recall silently
        returns nothing: a compliant model echoing what it was shown matches
        no memory at all.
        """
        return os.path.basename(self.path)

    def manifest_line(self) -> str:
        tag = f"[{self.type}] " if self.type else ""
        age = age_text(self.mtime_ms) if self.mtime_ms > 0 else "unknown age"
        return f"- {tag}{self.filename} ({age}): {self.description}"


def age_days(mtime_ms: float, now_ms: float | None = None) -> int:
    """Floor-rounded days. Clock skew (future mtime) clamps to 0."""
    now = now_ms if now_ms is not None else time.time() * 1000
    return max(0, int((now - mtime_ms) // DAY_MS))


def age_text(mtime_ms: float, now_ms: float | None = None) -> str:
    """Human units, not ISO timestamps.

    Models are poor at date arithmetic: a raw timestamp does not trigger
    staleness reasoning the way "47 days ago" does.
    """
    d = age_days(mtime_ms, now_ms)
    return "today" if d == 0 else "yesterday" if d == 1 else f"{d} days ago"


def freshness_warning(mtime_ms: float, now_ms: float | None = None) -> str:
    """Staleness caveat for memories older than a day. Empty when fresh.

    Warning on a memory written this morning is noise, and noise is how
    warnings stop being read.
    """
    d = age_days(mtime_ms, now_ms)
    if d <= 1:
        return ""
    return (
        f"This memory is {d} days old. Memories are point-in-time observations, "
        "not live state — claims about code behaviour or file:line citations may "
        "be outdated. Verify against current code before asserting as fact."
    )


# --------------------------------------------------------------------------
# Reading the store
# --------------------------------------------------------------------------

_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


def _parse_header(path: str) -> MemoryHeader | None:
    try:
        stat = os.stat(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            # Only the head is needed: the header is the index entry, and
            # reading whole bodies to build a manifest defeats the ladder.
            # Read enough that a long-but-legal frontmatter block still closes;
            # a truncated block silently falls back to garbage descriptions.
            head = fh.read(16384)
    except OSError:
        return None
    fm: dict[str, str] = {}
    if (m := _FM.match(head)) is not None:
        for line in m.group(1).splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip().strip("'\"")
    name = fm.get("name") or os.path.splitext(os.path.basename(path))[0]
    mtype = fm.get("type") if fm.get("type") in MEMORY_TYPES else None
    description = fm.get("description", "").strip()
    if not description:
        body = m.group(2) if m else head
        description = next(
            (ln.strip() for ln in body.splitlines()
             if ln.strip() and not ln.startswith("#")),
            "",
        )
    return MemoryHeader(path, name, description[:300], mtype, stat.st_mtime * 1000)


def scan(memory_dir: str, exclude_entrypoint: bool = True) -> list[MemoryHeader]:
    """Read every memory's header. Cheap: 4 KB per file, no bodies."""
    if not os.path.isdir(memory_dir):
        return []
    out = []
    for entry in sorted(os.listdir(memory_dir)):
        if not entry.endswith(".md"):
            continue
        if exclude_entrypoint and entry == ENTRYPOINT_NAME:
            continue          # already loaded in the prompt; never re-recall it
        if (h := _parse_header(os.path.join(memory_dir, entry))) is not None:
            out.append(h)
    return out


def manifest(headers: Sequence[MemoryHeader]) -> str:
    return "\n".join(h.manifest_line() for h in headers)


SELECTOR_PROMPT = """You are selecting memories that will be useful as the \
agent processes a user's query. You are given the query and a list of memory \
files with their names, types, ages and descriptions.

Return the filenames exactly as shown in the list (including the .md \
extension) for memories that will CLEARLY be useful — at most {max_recalled}.
- If you are unsure whether a memory helps, leave it out. Be selective.
- If none are clearly useful, return an empty list.
- Do not select usage reference or API docs for tools the agent is already \
using — it is exercising them and does not need the manual. DO still select \
warnings, gotchas and known issues about those tools: active use is exactly \
when those matter."""


def select(
    query: str,
    headers: Sequence[MemoryHeader],
    choose: Callable[[str, str], Iterable[str]],
    already_surfaced: frozenset[str] = frozenset(),
    max_recalled: int = MAX_RECALLED,
) -> list[MemoryHeader]:
    """Pick memories worth reading in full.

    *choose* is your cheap-model call: ``(system_prompt, user_content) ->
    [filename]``. A small model over a manifest of one-line descriptions beats
    an embedding index here, because the judgement being made is "would this
    change what I do", which is semantic in the *task* rather than the text.

    Filtering ``already_surfaced`` BEFORE the call matters: otherwise the
    selector spends its small budget re-picking files the caller will discard.
    """
    candidates = [h for h in headers if h.path not in already_surfaced]
    if not candidates:
        return []
    picked = list(
        choose(
            SELECTOR_PROMPT.format(max_recalled=max_recalled),
            f"Query:\n{query}\n\nAvailable memories:\n{manifest(candidates)}",
        )
    )
    # Key on filename, which is unique on disk — `name` comes from unvalidated
    # frontmatter and two files may share it, which would make one of them
    # permanently unreachable.
    by_file = {h.filename: h for h in candidates}
    selected: list[MemoryHeader] = []
    seen: set[str] = set()
    for choice in picked:                     # preserve the model's ranking
        key = str(choice).strip()
        header = by_file.get(key) or by_file.get(f"{key}.md")
        if header is not None and header.path not in seen:
            seen.add(header.path)
            selected.append(header)
    return selected[:max_recalled]


def render_recalled(header: MemoryHeader, body: str) -> str:
    """Body plus its staleness caveat, ready to inject as a user-role message."""
    encoded = body.encode("utf-8")
    if len(encoded) > MAX_MEMORY_BYTES:
        # Truncate on a character boundary; a CJK memory was ~3x over the cap
        # it claimed to enforce when this counted characters.
        body = encoded[:MAX_MEMORY_BYTES].decode("utf-8", errors="ignore")
    warning = freshness_warning(header.mtime_ms)
    head = f"<memory name=\"{header.name}\" type=\"{header.type or 'untyped'}\" age=\"{age_text(header.mtime_ms)}\">"
    tail = "</memory>"
    return f"{head}\n{body}\n{warning}\n{tail}" if warning else f"{head}\n{body}\n{tail}"


@dataclass
class RecallBudget:
    """Cumulative per-session cap on injected memory bytes.

    A per-turn cap bounds one injection; over a long session the selector keeps
    surfacing *distinct* files and the total grows without bound. Past the cap,
    stop recalling: the most relevant memories are already in context.
    """

    max_bytes: int = MAX_SESSION_BYTES
    spent: int = 0
    surfaced: set[str] = field(default_factory=set)

    def exhausted(self) -> bool:
        return self.spent >= self.max_bytes

    def record(self, header: MemoryHeader, rendered: str) -> None:
        self.spent += len(rendered.encode("utf-8"))
        self.surfaced.add(header.path)


# --------------------------------------------------------------------------
# Writing to the store
# --------------------------------------------------------------------------

#: The rule that decides what belongs here at all. Everything below it is
#: detail; this line is the module.
NON_DERIVABILITY_RULE = (
    "Save only what cannot be recovered from the project itself. Code "
    "structure, architecture, past fixes and git history are derivable with "
    "grep and git — saving them creates a store that is always slightly wrong "
    "and never worth reading. Save what happened in a conversation that "
    "nobody wrote down."
)

TYPE_GUIDANCE = {
    "user": (
        "Who the user is: role, expertise, goals, preferences. Lets you pitch "
        "an explanation at the right level. Never record a negative judgement, "
        "and never record anything irrelevant to the work."
    ),
    "feedback": (
        "Guidance on HOW to work — corrections AND confirmations. Record both: "
        "if you only save corrections you will avoid past mistakes but drift "
        "away from approaches the user already validated, and grow overly "
        "cautious. Always include the why, so you can judge edge cases instead "
        "of following the rule blindly."
    ),
    "project": (
        "Ongoing work, goals, constraints, incidents not derivable from code "
        "or history. Convert relative dates to absolute at save time "
        "('Thursday' -> '2026-03-05') or the memory expires unreadably."
    ),
    "reference": (
        "Pointers to external resources: dashboards, tickets, runbooks, URLs. "
        "The pointer, not a copy — copies go stale silently."
    ),
}

#: `feedback` and `project` decay fastest and are most often misapplied, so
#: they carry a required body structure.
STRUCTURED_TYPES = frozenset({"feedback", "project"})


def validate_memory(
    name: str, description: str, mtype: str, body: str
) -> list[str]:
    """Check a memory before writing it. Returns problems, empty if fine."""
    problems = []
    if mtype not in MEMORY_TYPES:
        problems.append(f"type must be one of {', '.join(MEMORY_TYPES)}; got {mtype!r}")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        problems.append(f"name must be kebab-case; got {name!r}")
    if not description.strip():
        problems.append("description is required — it is the entire recall signal")
    if len(description) > 300:
        problems.append("description over 300 chars; it is an index entry, not the memory")
    if not body.strip():
        problems.append("body is empty")
    if mtype in STRUCTURED_TYPES:
        if "**Why:**" not in body:
            problems.append(
                f"{mtype} memories need a **Why:** line — without the reason you "
                "cannot judge when the guidance does not apply"
            )
        if "**How to apply:**" not in body:
            problems.append(f"{mtype} memories need a **How to apply:** line")
    if (hit := _relative_date(body)) is not None:
        problems.append(
            f"body contains a relative date ({hit!r}); convert it to an absolute "
            "date (e.g. '2026-03-05') so the memory stays interpretable later"
        )
    return problems


_WEEKDAY = r"(?:mon|tues|wednes|thurs|fri|satur|sun)day"
#: Weekday names are the canonical case in the rule itself — "Thursday ->
#: 2026-03-05" — and the original pattern caught none of the seven.
_RELATIVE_DATE = re.compile(
    r"\b("
    r"yesterday|tomorrow"
    rf"|(?:next|last|this)\s+(?:week|month|quarter|year|sprint|{_WEEKDAY})"
    rf"|(?:by|on|before|after|until)\s+{_WEEKDAY}"
    rf"|{_WEEKDAY}\b(?!\s*[,\s]\s*\d{{4}})"
    r"|in\s+\d+\s+(?:day|week|month|year)s?"
    r"|end\s+of\s+(?:the\s+)?(?:week|month|quarter|year)"
    r")\b",
    re.I,
)
#: Fenced code and inline code are prose about code, not scheduling claims.
_CODE = re.compile(r"```.*?```|`[^`]*`", re.S)


def _relative_date(body: str) -> str | None:
    prose = _CODE.sub(" ", body)
    m = _RELATIVE_DATE.search(prose)
    return m.group(0) if m else None


def render_memory(name: str, description: str, mtype: str, body: str) -> str:
    # A description containing a newline, a colon-space, or a leading marker
    # corrupts the block it is written into. Flatten and quote rather than
    # trusting the caller.
    description = " ".join(description.split())
    if any(ch in description for ch in ":#\"'") :
        description = '"' + description.replace('"', "'") + '"'
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {mtype}\n"
        "---\n\n"
        f"{body.rstrip()}\n"
    )


def truncate_entrypoint(raw: str) -> tuple[str, bool]:
    """Cap ``MEMORY.md`` by lines AND bytes, naming which cap fired.

    Two caps because either alone is insufficient: 200 long lines can be 197 KB,
    and a byte cap alone cuts mid-line. Line-truncate first (a natural
    boundary), then byte-truncate at the last newline before the cap.
    """
    text = raw.strip()
    lines = text.split("\n")
    truncated = False
    if len(lines) > MAX_ENTRYPOINT_LINES:
        lines = lines[:MAX_ENTRYPOINT_LINES]
        text = "\n".join(lines)
        truncated = True
        text += f"\n\n[truncated at {MAX_ENTRYPOINT_LINES} lines]"
    note = f"\n\n[truncated at {MAX_ENTRYPOINT_BYTES} bytes]"
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_ENTRYPOINT_BYTES:
        # Reserve room for the note, or the "capped" output is over the cap.
        room = MAX_ENTRYPOINT_BYTES - len(note.encode("utf-8"))
        cut = encoded[:room].decode("utf-8", errors="ignore")
        nl = cut.rfind("\n")
        text = (cut[:nl] if nl > 0 else cut) + note
        truncated = True
    return text, truncated
