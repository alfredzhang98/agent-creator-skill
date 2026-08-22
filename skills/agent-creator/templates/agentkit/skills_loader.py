"""Skills: capability whose body loads only when the model asks for it.

Implements reference 11 (progressive disclosure), ported from Claude Code
2.1.88 ``src/skills/loadSkillsDir.ts`` and ``src/tools/SkillTool/``.

A skill is a directory containing ``SKILL.md``. Its *index entry* (name + one
line) is always in context; its *body* loads on invocation; its *directory* is
announced by path so the model can read bundled references on demand. Cost goes
from ``O(N x body)`` per turn to ``O(N x line) + O(1 x body)``.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

INDEX_CONTEXT_FRACTION = 0.01     # the index is metadata, not content
CHARS_PER_TOKEN = 4
DEFAULT_CHAR_BUDGET = 8_000
MAX_ENTRY_DESC_CHARS = 250        # discovery only; the body loads on invoke
MIN_ENTRY_DESC_CHARS = 20         # below this, degrade to names-only

#: Frontmatter keys that do not require a permission prompt. An ALLOWLIST, so a
#: capability added to the format next month asks until someone reviews it.
SAFE_FRONTMATTER = frozenset({
    "name", "description", "when-to-use", "when_to_use", "version",
    "user-invocable", "disable-model-invocation", "paths", "argument-hint",
})


@dataclass(frozen=True, eq=False)
class Skill:
    # eq=False: a frozen dataclass containing a dict generates a __hash__ that
    # raises at runtime, so any set/dict use crashes at the first call site.
    # Identity semantics are also what conditional-activation removal needs.
    name: str                      # canonical = directory name
    description: str
    body: str
    base_dir: str
    source: str = "project"        # bundled | policy | user | project | remote
    user_invocable: bool = True
    model_invocable: bool = True
    paths: tuple[str, ...] = ()    # non-empty => conditional
    frontmatter: dict[str, Any] = field(default_factory=dict)

    @property
    def unsafe_keys(self) -> list[str]:
        return sorted(
            k for k, v in self.frontmatter.items()
            if k not in SAFE_FRONTMATTER and v not in (None, "", [], {})
        )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Minimal YAML-ish frontmatter: ``key: value``, ``key: [a, b]``, block
    lists, and block scalars.

    Deliberately total — a malformed line is skipped rather than dropping the
    whole skill. A skill that half-parses is still discoverable; a skill that
    throws is invisible, which is the worse failure.
    """
    m = _FM.match(text)
    if not m:
        return {}, text
    data: dict[str, Any] = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        val: Any = raw.strip()

        # Block scalars. `description: >-` is the idiomatic way to write the
        # one field that decides whether a skill is ever activated, so a parser
        # that reads it as the literal string ">-" makes the skill invisible
        # while looking like it worked. This one could not read its own
        # SKILL.md.
        if val and val[0] in "|>" and set(val[1:]) <= set("+-0123456789"):
            folded, chomp = val[0] == ">", ("-" if "-" in val else
                                            "+" if "+" in val else "")
            indent = len(line) - len(line.lstrip())
            body: list[str] = []
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                    break
                body.append(nxt.strip() if folded else nxt[indent + 2:]
                            if len(nxt) > indent + 2 else "")
                i += 1
            while body and not body[-1].strip():
                body.pop()
            joined = " ".join(w for w in body if w) if folded else "\n".join(body)
            data[key] = joined + ("\n" if chomp == "+" and body else "")
            continue

        val = val.strip("'\"")
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        elif val.lower() in ("true", "false"):
            val = val.lower() == "true"
        data[key] = val
    # Block-style lists (`key:` followed by `  - item` lines) are the more
    # idiomatic YAML form. Skipping them silently turned a CONDITIONAL skill
    # into an unconditional one — exactly the failure the feature prevents.
    data = _absorb_block_lists(m.group(1), data)
    return data, m.group(2)


def _absorb_block_lists(raw: str, data: dict[str, Any]) -> dict[str, Any]:
    current: str | None = None
    items: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and current:
            items.append(stripped[2:].strip().strip("'\""))
            continue
        if items and current:
            data[current] = items
            items = []
        if ":" in line and not line.lstrip().startswith("#"):
            key, _, rest = line.partition(":")
            current = key.strip() if not rest.strip() else None
    if items and current:
        data[current] = items
    return data


def _as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
    elif isinstance(value, str):
        items = [v.strip() for v in value.split(",") if v.strip()]
    else:
        return ()
    # A pattern of only ** is not a conditional skill, it is an unconditional
    # one wearing a costume. Collapse it rather than pretending.
    items = [i[:-3] if i.endswith("/**") else i for i in items]
    return () if all(i == "**" for i in items) else tuple(items)


def load_skill(directory: str, source: str = "project") -> Skill | None:
    manifest = os.path.join(directory, "SKILL.md")
    if not os.path.isfile(manifest):
        return None                       # directory form only, by design
    try:
        with open(manifest, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    fm, body = parse_frontmatter(text)
    name = os.path.basename(os.path.normpath(directory))
    description = str(fm.get("description") or "").strip()
    if not description:
        # Fall back to the first prose line so a skill without frontmatter is
        # still matchable rather than silently unusable.
        description = next(
            (ln.strip() for ln in body.splitlines()
             if ln.strip() and not ln.startswith("#")),
            f"Skill: {name}",
        )
    return Skill(
        name=name,
        description=description,
        body=body,
        base_dir=os.path.abspath(directory),
        source=source,
        user_invocable=_as_bool(fm.get("user-invocable"), True),
        model_invocable=not _as_bool(fm.get("disable-model-invocation"), False),
        paths=_as_list(fm.get("paths")),
        frontmatter=fm,
    )


def load_dir(root: str, source: str = "project") -> list[Skill]:
    if not os.path.isdir(root):
        return []
    out = []
    for entry in sorted(os.listdir(root)):
        skill = load_skill(os.path.join(root, entry), source)
        if skill is not None:
            out.append(skill)
    return out


def load_all(roots: Sequence[tuple[str, str]]) -> tuple[list[Skill], list[Skill]]:
    """Load every root in precedence order; return (unconditional, conditional).

    Deduplicated by *resolved real path*, not by name: the same skill reached
    through a symlink and through an overlapping parent would otherwise consume
    the index budget twice and pick a winner non-deterministically. Real path,
    never inode — inode is unreliable on virtual, NFS and ExFAT filesystems.
    """
    seen: set[str] = set()
    uncond: list[Skill] = []
    cond: list[Skill] = []
    for root, source in roots:
        for skill in load_dir(root, source):
            try:
                identity = os.path.realpath(os.path.join(skill.base_dir, "SKILL.md"))
            except OSError:
                identity = skill.base_dir
            if identity in seen:
                continue
            seen.add(identity)
            (cond if skill.paths else uncond).append(skill)
    return uncond, cond


def matches_gitignore(rel_path: str, pattern: str) -> bool:
    """Gitignore-style match, which `fnmatch` is NOT.

    Three differences matter, and the first two go in opposite directions —
    which is why substituting `fnmatch` silently both over- and under-matches:

    * ``*`` does not cross ``/`` — ``src/*.py`` must not match ``src/a/b.py``
    * a pattern with no ``/`` matches at any depth — ``foo.py`` matches
      ``pkg/foo.py``
    * a trailing ``/`` (or a bare directory name) matches everything beneath it
    """
    rel = rel_path.replace(os.sep, "/").lstrip("./")
    pat = pattern.replace(os.sep, "/").strip()
    if not pat:
        return False
    anchored = pat.startswith("/")
    pat = pat.lstrip("/")
    dir_only = pat.endswith("/")
    pat = pat.rstrip("/")

    def seg_match(p: str, t: str) -> bool:
        # Translate one path against one pattern, honouring ** vs *.
        import re as _re
        rx, i = [], 0
        while i < len(p):
            if p.startswith("**/", i):
                rx.append("(?:.*/)?"); i += 3
            elif p.startswith("**", i):
                rx.append(".*"); i += 2
            elif p[i] == "*":
                rx.append("[^/]*"); i += 1
            elif p[i] == "?":
                rx.append("[^/]"); i += 1
            else:
                rx.append(_re.escape(p[i])); i += 1
        return _re.fullmatch("".join(rx), t) is not None

    if dir_only or "/" not in pat:
        # Directory patterns and bare names match the path or any prefix of it.
        parts = rel.split("/")
        for i in range(len(parts)):
            head = "/".join(parts[: i + 1])
            if seg_match(pat, head if anchored or "/" in pat else parts[i]):
                return True
            if not anchored and "/" not in pat and seg_match(pat, parts[i]):
                return True
        return False
    if seg_match(pat, rel):
        return True
    if not anchored:
        parts = rel.split("/")
        return any(seg_match(pat, "/".join(parts[i:])) for i in range(1, len(parts)))
    return False


def activate_for_paths(
    conditional: list[Skill], touched: Iterable[str], cwd: str
) -> list[Skill]:
    """Move conditional skills into the index when a matching file is touched.

    Mutates *conditional*, returning what was activated — so a skill activates
    at most once per session and the caller can log the transition.
    """
    activated = []
    for skill in list(conditional):
        for path in touched:
            try:
                rel = os.path.relpath(path, cwd)
            except ValueError:
                continue
            if rel.startswith("..") or os.path.isabs(rel):
                continue              # outside cwd can never match cwd-relative globs
            if any(matches_gitignore(rel, p) for p in skill.paths):
                # Remove by identity: two structurally identical Skills can
                # coexist (dedup is by realpath), and equality-based removal
                # would delete the wrong one.
                for i, cand in enumerate(conditional):
                    if cand is skill:
                        conditional.pop(i)
                        break
                activated.append(skill)
                break
    return activated


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------

def char_budget(context_window_tokens: int | None) -> int:
    if not context_window_tokens:
        return DEFAULT_CHAR_BUDGET
    return int(context_window_tokens * CHARS_PER_TOKEN * INDEX_CONTEXT_FRACTION)


def render_index(
    skills: Sequence[Skill],
    context_window_tokens: int | None = None,
    protected_sources: frozenset[str] = frozenset({"bundled"}),
    on_truncate: Any = None,
) -> str:
    """Full entries if they fit; otherwise degrade, protecting first-party.

    Degradation is staged rather than uniform, and *always reported*: silent
    truncation reads as "the model ignored my skill" when it was never shown
    one. A names-only entry is still invokable, so the floor is "less
    matchable", never "invisible".
    """
    if not skills:
        return ""
    budget = char_budget(context_window_tokens)
    desc = lambda s: s.description[:MAX_ENTRY_DESC_CHARS]
    full = [f"- {s.name}: {desc(s)}" for s in skills]
    if sum(map(len, full)) + len(full) - 1 <= budget:
        return "\n".join(full)

    protected = {i for i, s in enumerate(skills) if s.source in protected_sources}
    used = sum(len(full[i]) + 1 for i in protected)
    rest = [i for i in range(len(skills)) if i not in protected]
    if not rest:
        return "\n".join(full)

    overhead = sum(len(skills[i].name) + 4 for i in rest) + len(rest) - 1
    per_entry = (budget - used - overhead) // len(rest)

    if per_entry < MIN_ENTRY_DESC_CHARS:
        if on_truncate:
            on_truncate({"mode": "names_only", "skills": len(skills), "budget": budget})
        return "\n".join(
            full[i] if i in protected else f"- {skills[i].name}"
            for i in range(len(skills))
        )
    if on_truncate:
        on_truncate({"mode": "trimmed", "skills": len(skills), "per_entry": per_entry})
    return "\n".join(
        full[i] if i in protected else f"- {skills[i].name}: {desc(skills[i])[:per_entry]}"
        for i in range(len(skills))
    )


def render_body(skill: Skill, args: str = "") -> str:
    """Level 2 + the level-3 pointer.

    Announcing the base directory is what makes bundled reference files free
    until touched: the model reads them with the file tools it already has.
    """
    head = f"Base directory for this skill: {skill.base_dir}\n\n"
    body = skill.body.replace("${SKILL_DIR}", skill.base_dir)
    if args:
        body = body.replace("$ARGUMENTS", args)
    return head + body
