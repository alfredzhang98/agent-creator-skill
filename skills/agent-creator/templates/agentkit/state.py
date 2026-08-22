"""State: what survives the process, and how it is read back.

Distilled from Claude Code 2.1.88 ``src/utils/sessionStorage.ts`` (transcripts,
sidechains, agent metadata, resume) and Articraft's staging-then-promote
lifecycle (reference 08).

Three separable things get confused under the word "state", and conflating
them is why agent persistence layers rot:

1. **The transcript** — an append-only log of what happened. Never rewritten,
   flushed per event, and the only thing that makes a run resumable or
   auditable. Loss of the tail is acceptable; corruption of the middle is not.
2. **Metadata** — a small mutable record *about* a run (status, cwd, model,
   the description it started from). Written whole, atomically.
3. **Artifacts** — what the run produced. These stage somewhere disposable and
   are promoted only on success, so a crash leaves a mess in a scratch
   directory rather than a half-written result in the library.

The read side has one non-negotiable property: **tolerant readback**. A
transcript is written by the version of your agent that existed that day. If a
single unparseable line can abort a resume, every schema change silently
orphans every prior session.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator

#: Refuse to slurp a pathological transcript into memory.
MAX_TRANSCRIPT_READ_BYTES = 50 * 1024 * 1024


def new_session_id() -> str:
    return uuid.uuid4().hex


def sortable_id(prefix: str = "") -> str:
    """Time-ordered id: lexical sort == chronological sort.

    Worth the two extra lines. Directory listings, log greps and `ls` all sort
    lexically, and a run id that sorts wrongly makes every debugging session
    slightly worse forever.
    """
    return f"{prefix}{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Paths:
    """Where a session's state lives. One object so nothing hardcodes a join."""

    root: str
    session_id: str

    @property
    def session_dir(self) -> str:
        return os.path.join(self.root, self.session_id)

    @property
    def transcript(self) -> str:
        return os.path.join(self.session_dir, "transcript.jsonl")

    @property
    def metadata(self) -> str:
        return os.path.join(self.session_dir, "metadata.json")

    @property
    def tool_results(self) -> str:
        return os.path.join(self.session_dir, "tool-results")

    @property
    def staging(self) -> str:
        return os.path.join(self.session_dir, "staging")

    def sidechain(self, agent_id: str) -> str:
        """A subagent's transcript lives beside, not inside, the parent's.

        Interleaving a subagent's turns into the parent transcript makes both
        unreadable and makes resume ambiguous about who was speaking.
        """
        return os.path.join(self.session_dir, "subagents", f"{agent_id}.jsonl")

    def ensure(self) -> "Paths":
        os.makedirs(self.session_dir, exist_ok=True)
        os.makedirs(self.tool_results, exist_ok=True)
        return self


# --------------------------------------------------------------------------
# Atomic whole-file writes (metadata, artifacts)
# --------------------------------------------------------------------------

def write_atomic(path: str, content: str) -> None:
    """Temp file + rename. A crash leaves the old file, never a torn one."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        # The rename itself is not durable until the directory entry is
        # synced — the exact failure this function exists to prevent.
        try:
            dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path: str, data: Any) -> None:
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False, default=str))


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


# --------------------------------------------------------------------------
# The transcript
# --------------------------------------------------------------------------

@dataclass
class Transcript:
    """Append-only JSONL, flushed per event.

    Flush-per-event rather than buffered: the events you most need are the ones
    written immediately before the crash you are debugging.
    """

    path: str
    #: fsync every event. Off by default: flush() already survives a process
    #: crash, which is the failure this log exists for.
    durable: bool = False
    _fh: Any = field(default=None, repr=False)

    def open(self) -> "Transcript":
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        return self

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        entry = {
            "uuid": uuid.uuid4().hex,
            "ts": time.time(),
            "kind": kind,
            **payload,
        }
        if self._fh is None:
            self.open()
        self._fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        if self.durable:
            # flush() survives a process crash; fsync() survives the machine.
            # Off by default because it costs a syscall per event.
            os.fsync(self._fh.fileno())
        return entry

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "Transcript":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()


def read_transcript(
    path: str,
    on_bad_line: Callable[[int, str, Exception], None] | None = None,
    max_bytes: int = MAX_TRANSCRIPT_READ_BYTES,
) -> Iterator[dict[str, Any]]:
    """Read a transcript, skipping what it cannot parse.

    Tolerance is the whole point. A transcript was written by whatever version
    of the agent existed that day, and its tail may be a half-written line from
    a kill -9. One bad line must cost you that line, not the session.

    This streams, so there is no size limit to enforce — ``max_bytes`` is kept
    only to warn about a file large enough that the *caller* probably wants to
    know before draining it.
    """
    if not os.path.isfile(path):
        return
    if on_bad_line and os.path.getsize(path) > max_bytes:
        on_bad_line(0, f"transcript is {os.path.getsize(path):,} bytes", ValueError("large"))
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                if on_bad_line:
                    on_bad_line(lineno, line, exc)
                continue
            if isinstance(entry, dict):
                yield entry


# --------------------------------------------------------------------------
# Run metadata
# --------------------------------------------------------------------------

@dataclass
class RunMetadata:
    """Small, mutable, written whole. Every field has a default.

    Total defaults are not laziness: a metadata file written three versions ago
    must still load, or resume and audit both break on every schema change.
    """

    session_id: str = ""
    status: str = "running"        # running | completed | failed | aborted
    started_at: float = 0.0
    ended_at: float | None = None
    cwd: str = ""
    model: str = ""
    description: str = ""
    stop_reason: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    #: Everything needed to reproduce this run later.
    provenance: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> "RunMetadata":
        raw = read_json(path, {}) or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: str) -> None:
        write_json(path, asdict(self))


# --------------------------------------------------------------------------
# Staging then promote
# --------------------------------------------------------------------------

class Staging:
    """Artifacts are built somewhere disposable and moved only on success.

    The invariant this buys: the durable store contains only outputs that were
    verified. A crash mid-run leaves a staging directory you can inspect, not a
    half-written artifact indistinguishable from a good one.
    """

    def __init__(self, paths: Paths):
        self.dir = paths.staging
        os.makedirs(self.dir, exist_ok=True)
        self.promoted = False

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def promote(self, destination: str) -> str:
        """Move the staged tree to its final home, atomically at the top level."""
        os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
        if os.path.exists(destination):
            raise FileExistsError(
                f"{destination} already exists — promotion must never overwrite "
                "a committed artifact. Use a new destination or delete "
                "deliberately."
            )
        try:
            os.replace(self.dir, destination)
        except OSError:
            # Cross-device, or a non-empty target: fall back to copy+remove.
            # `os.replace` on a directory is atomic only within one filesystem,
            # and staging frequently lives on a different one from the library.
            shutil.copytree(self.dir, destination)
            shutil.rmtree(self.dir, ignore_errors=True)
        self.promoted = True
        return destination

    def abandon(self) -> str:
        """Leave the staging dir in place for debugging, and say where it is."""
        return self.dir


# --------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------

def resume_messages(
    transcript_path: str,
    on_bad_line: Callable[[int, str, Exception], None] | None = None,
) -> list[dict[str, Any]]:
    """Rebuild a message list from a transcript, dropping orphaned tool calls.

    An assistant message whose tool_use has no matching tool_result crashes the
    next API call on a malformed conversation. That happens routinely: a run
    killed between dispatching a tool and recording its result leaves exactly
    that shape. Repair on read rather than trusting the writer.
    """
    messages: list[dict[str, Any]] = []
    for entry in read_transcript(transcript_path, on_bad_line):
        if entry.get("kind") == "message" and isinstance(entry.get("message"), dict):
            messages.append(entry["message"])
    return drop_orphan_tool_calls(messages)


def _tool_call_ids(message: dict[str, Any]) -> list[str]:
    """Tool-call ids in either dialect.

    Reading only the OpenAI-shaped `tool_calls` array means every
    Anthropic-shaped orphan (`content: [{"type": "tool_use", ...}]`) passes
    through untouched — which is the format the upstream transcripts this is
    modelled on actually use.
    """
    ids = [c.get("id") for c in message.get("tool_calls") or [] if isinstance(c, dict)]
    content = message.get("content")
    if isinstance(content, list):
        ids += [
            b.get("id") for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
        ]
    return [i for i in ids if i]


def _tool_result_ids(message: dict[str, Any]) -> set[str]:
    content = message.get("content")
    if not isinstance(content, list):
        return set()
    return {
        b.get("tool_use_id") for b in content
        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id")
    }


def _without_results(message: dict[str, Any], drop: set[str]) -> dict[str, Any] | None:
    """Strip named tool_result blocks; return None if nothing is left."""
    content = message.get("content")
    if not isinstance(content, list):
        return message
    kept = [
        b for b in content
        if not (isinstance(b, dict) and b.get("type") == "tool_result"
                and b.get("tool_use_id") in drop)
    ]
    if not kept:
        return None
    return {**message, "content": kept}


def drop_orphan_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repair a resumed transcript into a conversation the API will accept.

    Three failure shapes, all produced routinely by a run killed mid-turn, and
    all of which make the *next* API call fail rather than the resume:

    1. A tool_use with no result — drop the whole assistant message, because
       partially satisfying a batch is as malformed as satisfying none of it.
    2. A tool_result whose tool_use we just dropped (or never had) — strip it,
       or we have merely traded one orphan for another.
    3. A tool_result appearing *before* its tool_use — position matters to the
       API, so satisfaction must be checked in order, not set-wise.
    """
    # Pass 1: which calls are satisfied by a LATER result?
    satisfied: set[str] = set()
    seen_calls: set[str] = set()
    for m in messages:
        for cid in _tool_call_ids(m):
            seen_calls.add(cid)
        for rid in _tool_result_ids(m):
            if rid in seen_calls:            # only counts if the call came first
                satisfied.add(rid)

    # Pass 2: drop unsatisfied assistant turns, collecting the ids they owned.
    kept: list[dict[str, Any]] = []
    orphaned_results: set[str] = set()
    for m in messages:
        ids = _tool_call_ids(m)
        if ids and not all(i in satisfied for i in ids):
            orphaned_results.update(ids)
            continue
        kept.append(m)

    # Pass 3: strip results that now point at nothing, and results that
    # preceded their call.
    live_calls = {i for m in kept for i in _tool_call_ids(m)}
    out: list[dict[str, Any]] = []
    for m in kept:
        rids = _tool_result_ids(m)
        stale = {r for r in rids if r not in live_calls or r not in satisfied}
        if stale:
            trimmed = _without_results(m, stale)
            if trimmed is None:
                continue
            m = trimmed
        out.append(m)
    return out
