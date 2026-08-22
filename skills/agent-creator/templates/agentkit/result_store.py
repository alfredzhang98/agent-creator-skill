"""Large tool results go to disk, not through a truncator.

Distilled from Claude Code 2.1.88 ``src/utils/toolResultStorage.ts`` and
``src/constants/toolLimits.ts``.

Truncation destroys information the model may need and gives it no way to get
it back. Persisting relocates it: the model receives a bounded preview plus a
path, and can read the rest on demand with the file tools it already has.

Two budgets, because they fail differently:
  * per result  — one enormous result (a 40 MB log)
  * per message — N parallel results, each individually legal, that together
                  bury the turn (Claude Code: constants/toolLimits.ts:36-49)
"""
from __future__ import annotations

import hashlib
import math
import os
import uuid
from dataclasses import dataclass
from typing import Any

#: Aggregate cap across ONE turn's batch of tool_result blocks.
MAX_RESULTS_PER_MESSAGE_CHARS = 200_000
#: How much of a persisted result the model sees inline.
PREVIEW_CHARS = 2_000

OPEN_TAG = "<persisted-output>"
CLOSE_TAG = "</persisted-output>"


@dataclass(frozen=True)
class Persisted:
    path: str
    preview: str
    total_chars: int


def _preview(content: str, limit: int = PREVIEW_CHARS) -> str:
    """Cut at a newline boundary when one is reasonably close to the limit."""
    if len(content) <= limit:
        return content
    window = content[:limit]
    nl = window.rfind("\n")
    return window[: nl + 1] if nl > limit // 2 else window


def _safe_stem(tool_use_id: str) -> str:
    """Derive a filename that cannot escape the results directory.

    Tool-use ids come from the model and the provider, so they are untrusted
    input to a path join. A id of ``../../x`` otherwise writes outside the
    directory — verified, not theoretical. Hashing also removes the collision
    that plain truncation introduces for ids sharing a long prefix.
    """
    raw = tool_use_id or uuid.uuid4().hex
    safe = "".join(c for c in raw if c.isalnum() or c in "-_")[:40]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{safe}-{digest}" if safe else digest


def persist(content: str, results_dir: str, tool_use_id: str) -> Persisted:
    os.makedirs(results_dir, exist_ok=True)
    path = os.path.join(results_dir, f"{_safe_stem(tool_use_id)}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return Persisted(path=path, preview=_preview(content), total_chars=len(content))


def render_persisted(p: Persisted) -> str:
    """The message the model gets in place of the content.

    It must say three things: that content was moved (not lost), where it is,
    and how much there is — otherwise the model treats the preview as the whole
    answer.
    """
    return (
        f"{OPEN_TAG}\n"
        f"Output too large ({p.total_chars:,} chars). Full output saved to: {p.path}\n\n"
        f"Preview (first {len(p.preview):,} chars):\n{p.preview}\n"
        f"{CLOSE_TAG}"
    )


def apply_result_cap(
    content: str, cap: float, results_dir: str, tool_use_id: str
) -> str:
    """Inline the content, or persist it and return the preview message."""
    if math.isinf(cap) or len(content) <= cap:
        return content
    return render_persisted(persist(content, results_dir, tool_use_id))


def apply_message_budget(
    blocks: list[dict[str, Any]],
    results_dir: str,
    budget: int = MAX_RESULTS_PER_MESSAGE_CHARS,
    exempt_indices: frozenset[int] = frozenset(),
) -> tuple[list[dict[str, Any]], bool]:
    """Persist the largest blocks in one turn's batch until under *budget*.

    Returns ``(blocks, within_budget)``. The flag is not decoration: if the
    only blocks left are exempt or too small to be worth relocating, the
    budget is simply not achievable and the caller needs to know rather than
    assume the turn is bounded.

    Largest-first frees the most context per relocation. Exemption is by
    *index* rather than by a field on the block, because a wire block may
    carry only API-legal keys.
    """
    def size(b: dict[str, Any]) -> int:
        c = b.get("content")
        return len(c) if isinstance(c, str) else _estimate_nontext(c)

    total = sum(size(b) for b in blocks)
    if total <= budget:
        return blocks, True

    out = list(blocks)
    order = sorted(
        (i for i in range(len(out)) if i not in exempt_indices),
        key=lambda i: size(out[i]),
        reverse=True,
    )
    for i in order:
        if total <= budget:
            break
        content = out[i].get("content")
        if not isinstance(content, str):
            continue
        replacement = render_persisted(
            persist(content, results_dir, out[i].get("tool_use_id", ""))
        )
        # Relocation adds framing. For a block only slightly over the preview
        # size the "replacement" is LONGER than the original — persisting it
        # writes a file, grows the message, and never converges. Only take the
        # swap when it actually helps.
        if len(replacement) >= len(content):
            try:
                os.unlink(persist_path(out[i].get("tool_use_id", ""), results_dir))
            except OSError:
                pass
            continue
        total -= len(content) - len(replacement)
        out[i] = {**out[i], "content": replacement}
    return out, total <= budget


def persist_path(tool_use_id: str, results_dir: str) -> str:
    """Where :func:`persist` would write this id. Exposed for cleanup."""
    return os.path.join(results_dir, f"{_safe_stem(tool_use_id)}.txt")


def _estimate_nontext(content: Any) -> int:
    """Approximate the context cost of a non-string tool_result payload.

    Real results carry lists of text/image blocks. Counting them as zero — the
    previous behaviour — makes an image-heavy turn invisible to the budget
    whose entire job is bounding the turn.
    """
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                total += len(str(part))
            elif part.get("type") == "text":
                total += len(part.get("text") or "")
            else:
                total += 1_500        # a rendered image/document, order of magnitude
        return total
    return len(str(content or ""))
