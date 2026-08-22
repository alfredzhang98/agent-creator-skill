"""Patch — multi-hunk editing in one call.

Adapted from Articraft `agent/tools/apply_patch.py` (Apache-2.0), which uses
the `*** Begin Patch` envelope several model families are trained on.

Why this exists alongside Edit: three related changes in one file cost three
Edit calls, three permission decisions, and three chances for the file to
shift underneath the model between them. A patch is one atomic decision over
one snapshot.

Why it is *not* a replacement for Edit: a patch is harder to get right, and
its failure modes are worse — a hunk that matches in the wrong place edits the
wrong code silently. So this applies the same rule Edit does, harder: **every
hunk must locate exactly one place**, and ambiguity is a refusal.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from contract import Context, ToolResult, Validation, build_tool

BEGIN = "*** Begin Patch"
END = "*** End Patch"
UPDATE = "*** Update File: "


@dataclass
class Hunk:
    """One change: context lines, removals, additions — in order."""

    lines: list[tuple[str, str]]     # (marker, text) with marker in " +-"

    @property
    def before(self) -> list[str]:
        return [t for m, t in self.lines if m in " -"]

    @property
    def after(self) -> list[str]:
        return [t for m, t in self.lines if m in " +"]

    @property
    def is_pure_insert(self) -> bool:
        return all(m == "+" for m, _ in self.lines)


def parse_patch(text: str) -> tuple[str, list[Hunk]]:
    """Parse the envelope. Raises ValueError with an actionable message.

    Deliberately strict about the envelope and about hunk markers: a patch
    that parses loosely is a patch that applies somewhere unintended.
    """
    raw = text.strip("\n")
    if not raw.strip():
        raise ValueError("patch is empty")
    lines = raw.split("\n")
    if lines[0].strip() != BEGIN:
        raise ValueError(f"patch must start with {BEGIN!r}")
    if lines[-1].strip() != END:
        raise ValueError(f"patch must end with {END!r}")

    body = lines[1:-1]
    path: str | None = None
    hunks: list[Hunk] = []
    current: list[tuple[str, str]] | None = None

    for line in body:
        if line.startswith("*** "):
            if line.startswith(UPDATE):
                if path is not None:
                    raise ValueError(
                        "only one file per patch; send separate patches so each "
                        "one can be approved and applied on its own"
                    )
                path = line[len(UPDATE):].strip()
            else:
                verb = line[4:].split(":")[0].strip()
                raise ValueError(
                    f"{verb!r} is not supported — this tool only updates an "
                    "existing file. Use Write to create one and a shell command "
                    "to move or delete."
                )
            continue
        if line.startswith("@@"):
            if path is None:
                raise ValueError(f"a hunk appeared before any {UPDATE!r} line")
            if current is not None and current:
                hunks.append(Hunk(current))
            current = []
            continue
        if current is None:
            raise ValueError(f"expected '@@' to start a hunk, got: {line[:60]!r}")
        if not line:
            current.append((" ", ""))
            continue
        marker, rest = line[0], line[1:]
        if marker not in " +-":
            raise ValueError(
                f"hunk lines must begin with ' ', '+' or '-'; got {line[:60]!r}"
            )
        current.append((marker, rest))

    if current:
        hunks.append(Hunk(current))
    if path is None:
        raise ValueError(f"patch has no {UPDATE!r} line")
    if not hunks:
        raise ValueError("patch has no hunks")
    for i, h in enumerate(hunks, 1):
        if not h.lines:
            raise ValueError(f"hunk {i} is empty")
        if h.is_pure_insert:
            raise ValueError(
                f"hunk {i} has only additions and no context lines, so there is "
                "no way to tell where it goes. Include the surrounding lines."
            )
    return path, hunks


def _locate(haystack: list[str], needle: list[str], start: int) -> list[int]:
    """Every index at or after *start* where *needle* occurs."""
    if not needle:
        return []
    return [
        i for i in range(start, len(haystack) - len(needle) + 1)
        if haystack[i:i + len(needle)] == needle
    ]


def apply_hunks(content: str, hunks: list[Hunk]) -> tuple[str, list[str]]:
    """Apply in order. Returns (new_content, problems); problems means no change.

    Each hunk is searched only from where the previous one ended, which makes
    a patch order-sensitive in the same way the file is — and stops hunk 3
    from matching text that hunk 1 already consumed.
    """
    lines = content.split("\n")
    out: list[str] = []
    cursor = 0
    problems: list[str] = []

    for i, hunk in enumerate(hunks, 1):
        matches = _locate(lines, hunk.before, cursor)
        if not matches:
            retry = _locate(lines, hunk.before, 0)
            if retry and retry[0] < cursor:
                problems.append(
                    f"hunk {i} matches earlier in the file than hunk {i-1} did. "
                    "Hunks must be in file order."
                )
            else:
                problems.append(
                    f"hunk {i} did not match. Its context lines are not present "
                    "as written — copy them exactly from a fresh Read."
                )
            continue
        if len(matches) > 1:
            problems.append(
                f"hunk {i} matches {len(matches)} places. Add surrounding "
                "context lines until it is unique."
            )
            continue
        at = matches[0]
        out.extend(lines[cursor:at])
        out.extend(hunk.after)
        cursor = at + len(hunk.before)

    if problems:
        return content, problems
    out.extend(lines[cursor:])
    return "\n".join(out), []


def _validate(input_: dict[str, Any], ctx: Context) -> Validation:
    try:
        rel, _hunks = parse_patch(input_["patch"])
    except ValueError as exc:
        return Validation.invalid(str(exc))
    path = rel if os.path.isabs(rel) else os.path.abspath(os.path.join(ctx.cwd, rel))
    if not os.path.isfile(path):
        return Validation.invalid(f"File does not exist: {path}")
    if path not in ctx.read_files:
        return Validation.invalid(
            f"{path} has not been read in this session. Call Read first so the "
            "hunk context matches the current contents."
        )
    if os.path.getmtime(path) > ctx.read_files[path]:
        return Validation.invalid(
            f"{path} changed on disk since you read it. Read it again — the "
            "hunks were written against contents that no longer exist."
        )
    return Validation.valid()


def _call(input_: dict[str, Any], ctx: Context) -> ToolResult:
    rel, hunks = parse_patch(input_["patch"])
    path = rel if os.path.isabs(rel) else os.path.abspath(os.path.join(ctx.cwd, rel))
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    updated, problems = apply_hunks(content, hunks)
    if problems:
        # All or nothing. A half-applied patch leaves the file in a state
        # neither the model nor the user asked for.
        return ToolResult.failure(
            f"Patch not applied ({len(problems)} of {len(hunks)} hunks failed); "
            "the file is unchanged.\n" + "\n".join(f"  - {p}" for p in problems),
            code="hunk_failed",
        )

    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(updated)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    ctx.read_files[path] = os.path.getmtime(path)
    return ToolResult.success(
        f"Applied {len(hunks)} hunk{'s' if len(hunks) > 1 else ''} to {path}",
        data={"path": path, "hunks": len(hunks)},
    )


PatchTool = build_tool(
    name="Patch",
    description=(
        "Apply several changes to one file in a single atomic edit.\n\n"
        "Format:\n"
        "```\n*** Begin Patch\n*** Update File: path/to/file.py\n@@\n"
        " unchanged context line\n-removed line\n+added line\n@@\n"
        " more context\n+another addition\n*** End Patch\n```\n\n"
        "- You MUST Read the file first; this tool fails otherwise.\n"
        "- Every hunk needs context lines and must match EXACTLY ONE place — "
        "ambiguity is refused rather than guessed.\n"
        "- Hunks apply in file order, and it is all-or-nothing: if any hunk "
        "fails, the file is left untouched and every failure is reported.\n"
        "- Prefer Edit for a single change; prefer this when three or more "
        "changes belong to one thought."
    ),
    search_hint="apply a multi-hunk patch to one file",
    input_schema={
        "type": "object",
        "properties": {"patch": {"type": "string", "description": "The patch envelope"}},
        "required": ["patch"],
        "additionalProperties": False,
    },
    call=_call,
    validate_input=_validate,
    rule_key=lambda i: (parse_patch(i["patch"])[0] if i.get("patch") else None),
    activity=lambda i: "Patching a file",
)
