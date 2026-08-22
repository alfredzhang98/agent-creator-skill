"""Glob and Grep — finding things without flooding the context.

Ported from Claude Code 2.1.88 (``src/tools/GlobTool``, ``src/tools/GrepTool``),
schemas from its published ``sdk-tools.d.ts``.

Search is the classic context-flooder, so both tools cap at the *tool*, not in
the prompt: Grep declares a result cap five times tighter than the default and
defaults to returning file paths rather than matching lines. A model that needs
the lines can ask for them; a model that gets 40,000 lines it did not ask for
has lost the turn either way.
"""
from __future__ import annotations

import fnmatch
import os
import re
from typing import Any

from contract import Context, Permission, ToolResult, Validation, build_tool

DEFAULT_HEAD_LIMIT = 250
MAX_FILE_BYTES = 5 * 1024 * 1024
#: Directories never worth walking. Skipping them is the difference between a
#: search that answers and one that times out in node_modules.
PRUNE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".next", "target", ".tox", ".ruff_cache",
}


def _walk(root: str, follow_links: bool = False):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_links):
        dirnames[:] = [d for d in dirnames if d not in PRUNE and not d.startswith(".claude-tmp")]
        for name in filenames:
            yield os.path.join(dirpath, name)


def _resolve_root(input_: dict[str, Any], ctx: Context) -> str:
    path = input_.get("path") or ctx.cwd
    return path if os.path.isabs(path) else os.path.abspath(os.path.join(ctx.cwd, path))


# --------------------------------------------------------------------------
# Glob
# --------------------------------------------------------------------------

def _glob_call(input_: dict[str, Any], ctx: Context) -> ToolResult:
    root = _resolve_root(input_, ctx)
    pattern = input_["pattern"]
    limit = int(input_.get("head_limit") or DEFAULT_HEAD_LIMIT)

    hits = []
    for path in _walk(root):
        rel = os.path.relpath(path, root)
        # Match the pattern against the relative path AND the bare filename, so
        # both "*.py" and "src/**/*.py" behave the way people expect.
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(os.path.basename(path), pattern):
            hits.append(path)

    if not hits:
        return ToolResult.success(
            f"No files matching {pattern!r} under {root}. "
            "Check the pattern (use ** to cross directories) or widen the path."
        )
    # Newest first: in a repository, recency is the best cheap relevance proxy.
    hits.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    shown, extra = hits[:limit], max(0, len(hits) - limit)
    body = "\n".join(shown)
    if extra:
        body += f"\n\n({extra} more matches not shown; narrow the pattern or raise head_limit)"
    return ToolResult.success(body, data={"count": len(hits)})


GlobTool = build_tool(
    name="Glob",
    description=(
        "Find files by name pattern. Returns absolute paths, newest first.\n\n"
        "- Patterns match the path relative to `path` and the bare filename.\n"
        "- Common build/vendor directories are skipped.\n"
        "- Use this rather than shelling out to `find`: the result is capped and "
        "the call is reviewable."
    ),
    search_hint="find files by name pattern or wildcard",
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": 'e.g. "*.py", "src/**/*.ts"'},
            "path": {"type": "string", "description": "Directory to search (default cwd)"},
            "head_limit": {"type": "integer", "description": "Max results"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    call=_glob_call,
    is_read_only=lambda _i: True,
    is_concurrency_safe=lambda _i: True,
    check_permissions=lambda i, c: Permission(Permission.ALLOW),
    max_result_chars=100_000,
    activity=lambda i: f"Searching for {i.get('pattern', '')}",
)


# --------------------------------------------------------------------------
# Grep
# --------------------------------------------------------------------------

def _grep_validate(input_: dict[str, Any], ctx: Context) -> Validation:
    try:
        re.compile(input_["pattern"])
    except re.error as exc:
        return Validation.invalid(
            f"Invalid regular expression {input_['pattern']!r}: {exc}. "
            "Escape regex metacharacters (. * + ? [ ] ( ) { } | ^ $ \\) to match them literally."
        )
    mode = input_.get("output_mode", "files_with_matches")
    if mode not in ("content", "files_with_matches", "count"):
        return Validation.invalid(
            f"output_mode must be one of content, files_with_matches, count (got {mode!r})"
        )
    return Validation.valid()


def _grep_call(input_: dict[str, Any], ctx: Context) -> ToolResult:
    root = _resolve_root(input_, ctx)
    rx = re.compile(input_["pattern"], 0 if input_.get("case_sensitive") else re.IGNORECASE)
    mode = input_.get("output_mode", "files_with_matches")
    glob_filter = input_.get("glob")
    limit = int(input_.get("head_limit") or DEFAULT_HEAD_LIMIT)
    before, after = int(input_.get("-B") or 0), int(input_.get("-A") or 0)

    paths = [root] if os.path.isfile(root) else list(_walk(root))
    if glob_filter:
        paths = [
            p for p in paths
            if fnmatch.fnmatch(os.path.basename(p), glob_filter)
            or fnmatch.fnmatch(os.path.relpath(p, root), glob_filter)
        ]

    per_file: list[tuple[str, list[tuple[int, str]]]] = []
    total = 0
    for path in paths:
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                continue
            with open(path, "r", encoding="utf-8", errors="strict") as fh:
                lines = fh.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue                    # unreadable or binary: silently skip
        found = [(i + 1, ln) for i, ln in enumerate(lines) if rx.search(ln)]
        if found:
            per_file.append((path, found))
            total += len(found)

    if not per_file:
        return ToolResult.success(
            f"No matches for {input_['pattern']!r} under {root}"
            + (f" (filtered to {glob_filter})" if glob_filter else "")
            + ". Try a looser pattern, or Glob first to confirm the files exist."
        )

    if mode == "files_with_matches":
        files = sorted((p for p, _ in per_file))[:limit]
        extra = max(0, len(per_file) - limit)
        body = "\n".join(files)
        if extra:
            body += f"\n\n({extra} more files matched)"
        return ToolResult.success(body, data={"files": len(per_file), "matches": total})

    if mode == "count":
        rows = sorted(((len(m), p) for p, m in per_file), reverse=True)[:limit]
        body = "\n".join(f"{n}\t{p}" for n, p in rows)
        return ToolResult.success(body, data={"files": len(per_file), "matches": total})

    # content
    chunks, emitted = [], 0
    for path, found in per_file:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        for lineno, _ in found:
            if emitted >= limit:
                break
            lo, hi = max(0, lineno - 1 - before), min(len(lines), lineno + after)
            block = "\n".join(f"{path}:{n + 1}:{lines[n]}" for n in range(lo, hi))
            chunks.append(block)
            emitted += 1
        if emitted >= limit:
            break
    body = ("\n--\n" if (before or after) else "\n").join(chunks)
    if total > emitted:
        body += f"\n\n({total - emitted} more matches not shown; raise head_limit or narrow the pattern)"
    return ToolResult.success(body, data={"files": len(per_file), "matches": total})


GrepTool = build_tool(
    name="Grep",
    description=(
        "Search file contents with a regular expression.\n\n"
        "- Defaults to `files_with_matches`: paths only. Ask for `content` when you "
        "actually need the lines, and pair it with -A/-B for context.\n"
        f"- Results are capped at {DEFAULT_HEAD_LIMIT}; the tail is reported, not silently dropped.\n"
        "- Binary and oversized files are skipped."
    ),
    search_hint="search file contents with regex",
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression"},
            "path": {"type": "string", "description": "File or directory (default cwd)"},
            "glob": {"type": "string", "description": 'Filename filter, e.g. "*.py"'},
            "output_mode": {
                "type": "string",
                "description": "content | files_with_matches | count",
            },
            "-A": {"type": "integer", "description": "Lines of trailing context"},
            "-B": {"type": "integer", "description": "Lines of leading context"},
            "case_sensitive": {"type": "boolean", "description": "Default false"},
            "head_limit": {"type": "integer", "description": "Max results"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    call=_grep_call,
    validate_input=_grep_validate,
    is_read_only=lambda _i: True,
    is_concurrency_safe=lambda _i: True,
    check_permissions=lambda i, c: Permission(Permission.ALLOW),
    # Five times tighter than the default: search output is the flooder.
    max_result_chars=20_000,
    activity=lambda i: f"Grepping for {i.get('pattern', '')}",
)
