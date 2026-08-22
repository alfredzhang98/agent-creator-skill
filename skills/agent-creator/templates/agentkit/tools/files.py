"""Read, Write and Edit — the three tools almost every agent needs.

Ported from Claude Code 2.1.88 (``src/tools/FileReadTool``, ``FileWriteTool``,
``FileEditTool``) with the authoritative input schemas from its published
``sdk-tools.d.ts``. Working code: these run as-is.

The design content is in the preconditions, not the I/O:

* **Read** numbers its output lines, so a later edit can cite exact lines, and
  self-bounds (2,000 lines / ~25k tokens) — which is what earns it the right to
  opt out of overflow-to-disk entirely.
* **Edit** refuses a non-unique ``old_string``. That single rule converts
  "silently edited the wrong occurrence" — an error the model cannot see — into
  a loud, self-correctable one.
* **Read-before-write** is enforced, not requested. The prompt says it and the
  tool checks it, so the model is never punished for obeying the description
  nor rewarded for ignoring it.
* **Staleness** is checked against mtime: if the file changed since the model
  read it, the edit is refused rather than silently clobbering another writer.
"""
from __future__ import annotations

import os
from typing import Any

from contract import (
    Context,
    Permission,
    ToolResult,
    Validation,
    build_tool,
)

MAX_LINES = 2_000
MAX_LINE_CHARS = 2_000
MAX_READ_BYTES = 10 * 1024 * 1024


def _abs(path: str, ctx: Context) -> str:
    return path if os.path.isabs(path) else os.path.abspath(os.path.join(ctx.cwd, path))


def _is_probably_binary(chunk: bytes) -> bool:
    return b"\x00" in chunk


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

def _read_validate(input_: dict[str, Any], ctx: Context) -> Validation:
    path = _abs(input_["file_path"], ctx)
    if os.path.isdir(path):
        return Validation.invalid(
            f"{path} is a directory, not a file. Use Glob to list its contents."
        )
    if not os.path.exists(path):
        # Name the likely fix. A bare "not found" makes the model guess.
        parent = os.path.dirname(path)
        hint = (
            f" The directory {parent} exists — check the filename."
            if os.path.isdir(parent)
            else f" The directory {parent} does not exist either."
        )
        return Validation.invalid(f"File does not exist: {path}.{hint}", code=2)
    if os.path.getsize(path) > MAX_READ_BYTES:
        return Validation.invalid(
            f"File is {os.path.getsize(path):,} bytes, over the {MAX_READ_BYTES:,} "
            "byte limit. Use offset/limit to read a range, or Grep to search it."
        )
    return Validation.valid()


def _read_call(input_: dict[str, Any], ctx: Context) -> ToolResult:
    path = _abs(input_["file_path"], ctx)
    offset = max(int(input_.get("offset") or 0), 0)
    limit = int(input_.get("limit") or MAX_LINES)

    with open(path, "rb") as fh:
        head = fh.read(8192)
    if _is_probably_binary(head):
        return ToolResult.failure(
            f"{path} appears to be a binary file and cannot be read as text.",
            code="binary",
        )

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    total = len(lines)
    window = lines[offset : offset + min(limit, MAX_LINES)]
    body = "".join(
        # 1-indexed and line-anchored: an Edit can quote these exactly.
        f"{offset + i + 1}\t{ln.rstrip(chr(10))[:MAX_LINE_CHARS]}\n"
        for i, ln in enumerate(window)
    )

    # Record the read so Edit/Write can enforce read-before-write and detect
    # a file that changed underneath the model.
    ctx.read_files[path] = os.path.getmtime(path)

    if not window:
        return ToolResult.success(
            f"(file has {total} lines; offset {offset} is past the end)"
        )
    shown = offset + len(window)
    footer = f"\n\n({shown} of {total} lines shown; read from offset {shown} for more)" \
        if shown < total else ""
    return ToolResult.success(body + footer, data={"path": path, "total_lines": total})


ReadTool = build_tool(
    name="Read",
    description=(
        "Read a file from the local filesystem by absolute path.\n\n"
        f"- Reads up to {MAX_LINES} lines by default; use offset/limit for a range.\n"
        "- Output is line-numbered (`N\\ttext`) so you can quote exact lines to Edit.\n"
        "  The number and tab are display only — never include them in an edit string.\n"
        "- Reading a missing file, a directory, or a binary file returns an error "
        "explaining what to do instead.\n"
        "- Do NOT re-read a file you just edited to verify: Edit fails loudly if it "
        "did not apply, so a confirming read only costs context."
    ),
    search_hint="read file contents by path",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the file"},
            "offset": {"type": "integer", "description": "0-based line to start at"},
            "limit": {"type": "integer", "description": "Number of lines to read"},
        },
        "required": ["file_path"],
        "additionalProperties": False,
    },
    call=_read_call,
    validate_input=_read_validate,
    is_read_only=lambda _i: True,
    is_concurrency_safe=lambda _i: True,
    # Reading is the one thing a coding agent does constantly and safely.
    check_permissions=lambda i, c: Permission(Permission.ALLOW),
    # Persisting a read result would create a Read -> file -> Read loop, and the
    # tool already self-bounds. This is the documented opt-out, not an oversight.
    max_result_chars=float("inf"),
    rule_key=lambda i: i.get("file_path"),
    activity=lambda i: f"Reading {os.path.basename(i.get('file_path', ''))}",
)


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------

def _require_fresh_read(path: str, ctx: Context) -> str | None:
    """Shared precondition for every mutating file tool."""
    if not os.path.exists(path):
        return None                                  # creating is always fine
    if path not in ctx.read_files:
        return (
            f"{path} exists but has not been read in this session. Call Read on "
            "it first so you are editing the current contents."
        )
    if os.path.getmtime(path) > ctx.read_files[path]:
        return (
            f"{path} has changed on disk since you read it. Read it again before "
            "writing, or your change will discard someone else's."
        )
    return None


def _write_validate(input_: dict[str, Any], ctx: Context) -> Validation:
    path = _abs(input_["file_path"], ctx)
    if os.path.isdir(path):
        return Validation.invalid(f"{path} is a directory.")
    if (err := _require_fresh_read(path, ctx)) is not None:
        return Validation.invalid(err, code=3)
    return Validation.valid()


def _write_call(input_: dict[str, Any], ctx: Context) -> ToolResult:
    path = _abs(input_["file_path"], ctx)
    existed = os.path.exists(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Write to a temp file and rename: a crash mid-write leaves the old file
    # intact rather than a truncated one.
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(input_["content"])
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    ctx.read_files[path] = os.path.getmtime(path)
    verb = "Updated" if existed else "Created"
    n = input_["content"].count("\n") + 1
    return ToolResult.success(f"{verb} {path} ({n} lines)", data={"path": path})


WriteTool = build_tool(
    name="Write",
    description=(
        "Write a file to the local filesystem, overwriting it if it exists.\n\n"
        "- If the file exists you MUST Read it first; this tool fails otherwise.\n"
        "- It also fails if the file changed on disk since you read it.\n"
        "- For a partial change, prefer Edit — overwriting a whole file to change "
        "three lines destroys anything you did not know was there."
    ),
    search_hint="create or overwrite a whole file",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to write"},
            "content": {"type": "string", "description": "Full file contents"},
        },
        "required": ["file_path", "content"],
        "additionalProperties": False,
    },
    call=_write_call,
    validate_input=_write_validate,
    is_destructive=lambda i: True,     # it can replace existing content
    rule_key=lambda i: i.get("file_path"),
    activity=lambda i: f"Writing {os.path.basename(i.get('file_path', ''))}",
)


# --------------------------------------------------------------------------
# Edit
# --------------------------------------------------------------------------

def _edit_validate(input_: dict[str, Any], ctx: Context) -> Validation:
    path = _abs(input_["file_path"], ctx)
    if not os.path.exists(path):
        return Validation.invalid(f"File does not exist: {path}. Use Write to create it.")
    if input_["old_string"] == input_["new_string"]:
        return Validation.invalid("old_string and new_string are identical — nothing to do.")
    if (err := _require_fresh_read(path, ctx)) is not None:
        return Validation.invalid(err, code=3)
    return Validation.valid()


def _edit_call(input_: dict[str, Any], ctx: Context) -> ToolResult:
    path = _abs(input_["file_path"], ctx)
    old, new = input_["old_string"], input_["new_string"]
    replace_all = bool(input_.get("replace_all"))

    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    count = content.count(old)
    if count == 0:
        hint = ""
        if old.strip() and old.strip() in content:
            hint = (
                " The text is present but the whitespace differs — copy it exactly "
                "from Read output, excluding the line-number prefix."
            )
        return ToolResult.failure(
            f"old_string not found in {path}.{hint}", code="not_found"
        )
    if count > 1 and not replace_all:
        return ToolResult.failure(
            f"old_string appears {count} times in {path}; the edit would be "
            "ambiguous. Include more surrounding context to make it unique, or "
            "pass replace_all: true to change every occurrence.",
            code="not_unique",
            data={"occurrences": count},
        )

    updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(updated)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    ctx.read_files[path] = os.path.getmtime(path)

    n = count if replace_all else 1
    return ToolResult.success(
        f"Applied {n} edit{'s' if n > 1 else ''} to {path}",
        data={"path": path, "replacements": n},
    )


EditTool = build_tool(
    name="Edit",
    description=(
        "Perform an exact string replacement in a file.\n\n"
        "- You MUST Read the file in this session first; this tool fails otherwise.\n"
        "- The edit FAILS if old_string is not unique. Add surrounding context to "
        "disambiguate, or pass replace_all to change every occurrence.\n"
        "- Strip the Read line-number prefix (`N\\t`) before matching, and preserve "
        "the exact indentation that follows it."
    ),
    search_hint="modify file contents in place",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to modify"},
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
        },
        "required": ["file_path", "old_string", "new_string"],
        "additionalProperties": False,
    },
    call=_edit_call,
    validate_input=_edit_validate,
    rule_key=lambda i: i.get("file_path"),
    activity=lambda i: f"Editing {os.path.basename(i.get('file_path', ''))}",
)
