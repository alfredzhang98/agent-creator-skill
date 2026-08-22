# 11. Skills: Progressive Disclosure of Capability

**Maps to:** Skills · System Prompt · Action space · State/Context · **Distilled from:** Claude Code 2.1.88 — `src/skills/loadSkillsDir.ts`, `src/tools/SkillTool/`, `src/skills/bundledSkills.ts`, `src/utils/toolSearch.ts`, `src/tools/ToolSearchTool/prompt.ts`

## Why this module exists

Context is the one resource an agent cannot buy more of mid-turn, and every
capability you *might* need costs tokens on *every* turn if it lives in the
prompt. A hundred domain playbooks, tool schemas, and house-style documents
turn into a five-figure token tax paid before the model reads the user's
first word — and a model that must attend to all of it at once picks worse
than one shown ten relevant lines.

A **skill** is the fix: a named, self-contained capability whose *index
entry* (name + one-line description) is always in context, and whose *body*
is loaded only when the model decides it applies. This turns capability cost
from `O(N × full_body)` per turn into `O(N × one_line) + O(1 × full_body)`.
The same mechanism generalises to tool schemas (deferred tools) and to
reference files (the skill body names a directory the model may `Read`).

The module is worth designing deliberately because the naive version fails in
three specific ways: the index grows until it *is* the tax it was meant to
avoid; a skill loaded from an untrusted directory becomes a prompt-injection
vector with the agent's full permissions; and a capability index that changes
mid-session invalidates the prompt cache on every mutation.

## How Claude Code implements it

### The three-level disclosure ladder

Level 1 is the listing — `- name: description` lines injected as a
system-reminder, one line per skill, capped as a fraction of the context
window (`tools/SkillTool/prompt.ts:70-171`). Level 2 is the body — the
`SKILL.md` markdown, returned only when the model calls the Skill tool
(`tools/SkillTool/SkillTool.ts:580+`). Level 3 is the skill's own directory:
the body is prefixed with `Base directory for this skill: <dir>`
(`skills/loadSkillsDir.ts:345-347`) so the model can `Read`/`Grep` bundled
references, scripts, and templates on demand — an unbounded payload that
costs nothing until touched.

### Skill = directory + SKILL.md + frontmatter contract

Only the directory form is accepted in a `skills/` root: `skill-name/SKILL.md`,
with the *directory name* as the canonical skill name; loose `.md` files are
skipped (`skills/loadSkillsDir.ts:407-480`, esp. `424-428`, `452`). The
frontmatter is a fixed vocabulary parsed in one place
(`skills/loadSkillsDir.ts:185-265`): `description`, `when_to_use`,
`allowed-tools`, `argument-hint`, `arguments`, `model`, `effort`, `version`,
`user-invocable`, `disable-model-invocation`, `context: fork`, `agent`,
`paths`, `hooks`, `shell`. Every field has a total default — a malformed or
absent field degrades to a documented value rather than dropping the skill,
and `description` falls back to the first prose extracted from the markdown
body (`skills/loadSkillsDir.ts:208-214`).

Two independent booleans control who may invoke: `user-invocable` (default
true; false hides it from the human's slash-command menu) and
`disable-model-invocation` (default false; true makes the Skill tool refuse
it with error code 4). They are separate because "a human may run this" and
"the model may run this on its own initiative" are genuinely different
grants.

### The listing budget: 1% of the context window

The skill index is allocated `SKILL_BUDGET_CONTEXT_PERCENT = 0.01` of the
context window, converted at 4 chars/token, defaulting to 8,000 chars
(`tools/SkillTool/prompt.ts:21-41`). Per entry, descriptions are hard-capped
at `MAX_LISTING_DESC_CHARS = 250` with the stated reason that *the listing is
for discovery only* — the body loads on invoke, so a verbose `whenToUse`
"waste[s] turn-1 cache_creation tokens without improving match rate"
(`tools/SkillTool/prompt.ts:25-29`).

Degradation under budget pressure is staged, not uniform
(`tools/SkillTool/prompt.ts:70-171`): try full entries; if over budget,
partition into bundled (first-party, never truncated) and the rest; compute a
per-entry description length from the remaining budget; if that falls below
`MIN_DESC_LENGTH = 20`, drop non-bundled entries to *names only* while
bundled entries keep full descriptions. A name-only entry is still
discoverable — the model can invoke it and read the body — so the graceful
floor is "less matchable", never "invisible".

### Discovery sources, precedence, and the settings gate

Skills load in parallel from five roots — managed/policy, user
(`~/.claude/skills`), project (`.claude/skills` walked up to home),
`--add-dir` extras, and the legacy `commands/` directory — plus bundled and
MCP sources (`skills/loadSkillsDir.ts:638-723`, `LoadedFrom` at `67-73`).
Each root is independently gated by policy: `isSettingSourceEnabled`,
`isRestrictedToPluginOnly('skills')`, and a `CLAUDE_CODE_DISABLE_POLICY_SKILLS`
env kill-switch (`skills/loadSkillsDir.ts:650-713`). `--bare` mode skips all
auto-discovery and loads only explicit `--add-dir` paths — with an explicit
note that bare mode is *not* a policy bypass (`skills/loadSkillsDir.ts:654-675`).

### Deduplication by realpath, never by name

The same skill reachable through a symlink and through an overlapping parent
directory would otherwise load twice and eat the budget twice. Identity is
`realpath()` of the `SKILL.md` file (`skills/loadSkillsDir.ts:107-124`),
computed for all candidates in parallel and then deduped synchronously so
first-source-wins ordering is deterministic (`skills/loadSkillsDir.ts:725-763`).
The comment records *why* realpath and not inode: inode 0 on some virtual /
container / NFS filesystems, precision loss on ExFAT.

### Dynamic discovery: skills found by walking up from touched files

A monorepo's per-package skills should not all load at startup. Instead,
whenever the agent touches a file, the harness walks from that file's
directory up to (but excluding) cwd looking for `.claude/skills`
(`skills/loadSkillsDir.ts:861-915`). Three details make it cheap and safe:
every checked path — hit *or* miss — is memoised in a set so the common
"directory doesn't exist" case is one failed `stat` per path per session, not
one per file operation (`878-883`); the prefix check is `cwd + separator` so
`/project-backup` never matches cwd `/project` (`876`); and a discovered
directory is skipped if `git check-ignore` says its parent is ignored, which
blocks `node_modules/pkg/.claude/skills` from silently loading (`886-897`).
Deeper directories win: load results are merged in reverse order so nearer
skills overwrite farther ones by name (`skills/loadSkillsDir.ts:944-951`).

### Conditional skills: activated by path glob, not by mention

A skill with `paths:` frontmatter is parsed into gitignore-style patterns
(`skills/loadSkillsDir.ts:159-178`) and held *out* of the listing entirely at
startup (`771-796`). It enters the listing only when a file the agent
operates on matches one of its patterns
(`skills/loadSkillsDir.ts:997-1058`), matched relative to cwd with the same
`ignore` library used for conditional `CLAUDE.md` rules. Patterns of only
`**` are treated as "no paths" — a match-all conditional is just an
unconditional skill, so it is loaded normally instead of pretending to be
conditional (`173-175`).

### Invocation is a tool call with typed refusal codes

The Skill tool's input is two strings: `skill` and optional `args`
(`tools/SkillTool/SkillTool.ts:291-298`). Its output schema is a *union* —
inline execution returns `{success, commandName, allowedTools, model,
status:'inline'}`; a forked skill returns `{..., status:'forked', agentId,
result}` (`tools/SkillTool/SkillTool.ts:301-326`) — so "ran in this context"
and "ran in a subagent" are distinguishable by the caller rather than
inferred from prose. `validateInput` returns numbered refusals before any
permission work: empty name (1), unknown skill (2), model-invocation disabled
(4), not a prompt-type command (5), undiscovered remote skill (6)
(`tools/SkillTool/SkillTool.ts:354-430`). Leading `/` is stripped for
compatibility and counted as telemetry rather than rejected (`366-372`).

The tool's own prompt closes the two failure modes that matter behaviourally:
*"When a skill matches the user's request, this is a BLOCKING REQUIREMENT:
invoke the relevant Skill tool BEFORE generating any other response"* and
*"NEVER mention a skill without actually calling this tool"*
(`tools/SkillTool/prompt.ts:188-195`). It also tells the model how to detect
that a skill is **already loaded** in the current turn (a `<command-name>`
tag is present) so it follows the instructions instead of re-invoking.

### Trust gating by property allowlist, not by blocklist

Skills are code-shaped data from arbitrary directories, so invoking one is
permission-checked like any other tool call (`tools/SkillTool/SkillTool.ts:432-578`):
deny rules first, then allow rules (both supporting `name` and `prefix:*`
forms), then a **safe-properties auto-allow**. A skill runs without a prompt
only if every property it declares is in `SAFE_SKILL_PROPERTIES`
(`tools/SkillTool/SkillTool.ts:875-933`) — an allowlist chosen precisely so
that *any property added to the format in future defaults to requiring
permission* until someone reviews it. A skill carrying `hooks`, for instance,
is not in the set, so it always asks. On `ask`, the dialog offers two
one-click rules (`skill` and `skill:*`) so the human's grant is scoped rather
than global (`540-567`).

### Untrusted sources never get shell injection

Skill bodies support inline shell injection (`` !`cmd` ``) and
`${CLAUDE_SKILL_DIR}` / `${CLAUDE_SESSION_ID}` substitution
(`skills/loadSkillsDir.ts:356-369`). MCP-sourced skills — remote and
untrusted — are hard-excluded from shell execution by source check, not by
sanitising the content (`skills/loadSkillsDir.ts:371-396`). When a
file-sourced skill *does* inject shell, the execution inherits only the
skill's own `allowed-tools` as temporary allow rules (`382-390`) — a
capability grant scoped to that one expansion.

### Bundled skills: compiled in, extracted on first use

First-party skills are registered programmatically from a typed definition
(`skills/bundledSkills.ts:15-41, 53-73`) rather than shipped as loose files.
Those carrying reference files extract them to disk lazily on first
invocation, memoising the *promise* so concurrent invocations await one
extraction instead of racing into duplicate writes
(`skills/bundledSkills.ts:59-73`), then prepend the same
`Base directory for this skill:` line — bundled and disk skills converge on
one contract.

### The same ladder, applied to tool schemas

Tools get progressive disclosure too. A tool is *deferred* — sent with
`defer_loading: true`, name announced but schema withheld — if it is an MCP
tool or declares `shouldDefer: true`, unless it declares `alwaysLoad`
(`tools/ToolSearchTool/prompt.ts:62-108`). The model recovers a schema by
calling ToolSearch with `select:Name1,Name2`, keyword terms, or `+required`
terms (`tools/ToolSearchTool/prompt.ts:44-51`). The tool that loads the
others is never itself deferred (`70-71`), and neither are tools whose prompt
carries a behavioural contract the model must see on turn 1 — the delegation
tool under fork-first, and the user-visible-output tool, are explicitly
exempted (`73-105`).

Deferral is threshold-driven, not all-or-nothing: mode `tst` always defers,
`standard` never does, and `tst-auto` defers only once deferrable schemas
exceed a percentage of the context window — default 10%
(`utils/toolSearch.ts:45-49, 155-172`).

**The measured negative result is as valuable as the mechanism**: rendering
each deferred tool's one-line `searchHint` in the announcement list was
A/B-tested and showed no benefit, so the announcement is the bare tool name
(`tools/ToolSearchTool/prompt.ts:110-117`). Progressive disclosure pays; the
extra description line at the index level did not.

### Volatile content must never live in the cached prefix

The list of available subagents used to be interpolated into the delegation
tool's *description*. Because MCP servers connecting asynchronously, plugin
reloads, and permission-mode changes all mutate that list, every mutation
changed a tool description and busted the entire tool-schema prompt cache —
measured at **~10.2% of fleet cache-creation tokens**. The fix was to move
the list out of the description and inject it as an attachment message
instead (`tools/AgentTool/prompt.ts:48-59`). The same reasoning shows up as
a one-line normalisation elsewhere: the per-UID temp directory in the shell
tool's prompt is rewritten to the literal `$TMPDIR` so the prompt text is
byte-identical across users and can share a cross-user global cache
(`tools/BashTool/prompt.ts:185-190`).

This is the load-bearing constraint on the whole module: **anything that
changes during a session belongs in the mutable tail, never in the cached
prefix** — which is exactly why the skill index is a system-reminder
attachment rather than part of the system prompt.

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Index always in context, body only on invoke, reference files only on `Read` | Turns capability cost from O(N × body) per turn into O(N × line); the third level is unbounded and free until touched | Matching quality now depends entirely on one line of description; a badly written `description` makes a good skill unreachable |
| Budget the index as a % of context (1%), not a fixed count | Scales with the model actually in use; keeps the tax proportional rather than absolute | A large skill library silently degrades to shorter descriptions — you must log truncation or you will not notice |
| Degrade bundled and third-party entries differently | First-party skills are the ones whose absence breaks core flows; sacrificing their descriptions first would be backwards | Encodes a trust hierarchy in the budget code; third-party authors compete for a smaller pool |
| Directory + `SKILL.md` only, name = directory name | One canonical identity for the skill, its base dir, and its references; no name/file drift | Cannot ship a one-file skill in a skills root; the legacy loose-file form survives only in the deprecated commands dir |
| Two separate invocability flags (`user-invocable`, `disable-model-invocation`) | "A human may run this" and "the model may run this unprompted" are different grants — dangerous skills often want exactly one | Two booleans to reason about; defaults must be memorised |
| Dedup by `realpath`, cache misses as well as hits | Symlinks and overlapping roots otherwise double-charge the budget; inode is unreliable on virtual/NFS/ExFAT filesystems | One `realpath` syscall per candidate at startup |
| Discover nested skills lazily by walking up from touched files | A monorepo's per-package skills load only when work actually reaches that package | Skills appear mid-session, so any prompt-cache prefix containing the index is invalidated at that moment |
| Gate nested discovery on `git check-ignore` | Stops `node_modules/**/.claude/skills` from injecting instructions from a dependency | Fails open outside a git repo — the invocation-time permission dialog is the real boundary, not this filter |
| Conditional skills activate on path match, not on keyword | Deterministic, cheap, and matches how conditional repo rules already work | A skill whose trigger is conceptual rather than path-shaped cannot use it |
| Permission-check skill invocation with a **safe-property allowlist** | New frontmatter capabilities default to "ask" instead of silently inheriting auto-allow | Every new field needs a review + allowlist edit, or every skill using it starts prompting |
| Refuse shell injection for MCP-sourced skills by source, not by sanitising | Sanitising untrusted markdown for shell metacharacters is a losing game; source is a fact you already have | Remote skills cannot ship dynamic content, even benign |
| Announce deferred tools by bare name (hints A/B'd away) | Measured: hints did not improve selection, and cost tokens on every turn | The model occasionally fetches a schema it does not need — one cheap round-trip |
| Never defer the discovery tool, or tools carrying turn-1 behavioural contracts | A deferred loader is unreachable; a deferred output-contract tool means the contract arrives after the model already spoke | Each exemption is a hand-maintained special case in one function |
| Volatile lists live in attachments, never in tool descriptions | A mutating description invalidates the whole tool-schema cache; the measured cost is in the constants table below | Two injection paths to keep in sync; the model sees the list in a different place than the tool it belongs to |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| `SKILL_BUDGET_CONTEXT_PERCENT` | `0.01` (`tools/SkillTool/prompt.ts:21`) | The index is discovery metadata, not content — 1% of context is the stated ceiling for it |
| `CHARS_PER_TOKEN` (skill budget) | `4` (`tools/SkillTool/prompt.ts:22`) | Char-based budgeting avoids tokenising the listing on every render |
| `DEFAULT_CHAR_BUDGET` | `8_000` (`tools/SkillTool/prompt.ts:23`) | Fallback when the context window is unknown: 1% of 200k × 4 |
| `MAX_LISTING_DESC_CHARS` | `250` (`tools/SkillTool/prompt.ts:29`) | Per-entry hard cap; longer text burns turn-1 cache-creation tokens without improving match rate |
| `MIN_DESC_LENGTH` | `20` (`tools/SkillTool/prompt.ts:68`) | Below this a description is noise — switch non-bundled entries to names-only instead |
| Skill file contract | `<dir>/SKILL.md`, name = dir name (`skills/loadSkillsDir.ts:424-452`) | One identity for skill, base dir, and references |
| `user-invocable` default | `true` (`skills/loadSkillsDir.ts:216-219`) | Skills exist to be run by humans; hiding is the opt-in |
| `disable-model-invocation` default | `false` (`skills/loadSkillsDir.ts:255-257`) | Model may self-invoke unless the author opts out |
| `paths: **` | treated as *no* paths (`skills/loadSkillsDir.ts:173-175`) | A match-all conditional is an unconditional skill; do not pretend otherwise |
| Nested-discovery bound | strictly below cwd, `cwd + sep` prefix test (`skills/loadSkillsDir.ts:866-876`) | cwd-level skills already load at startup; the separator stops `/project-backup` matching `/project` |
| Nested-discovery precedence | deepest directory wins (`skills/loadSkillsDir.ts:911-914, 944-951`) | Nearest skill to the file is the most specific |
| Skill-tool result cap | `maxResultSizeChars: 100_000` (`tools/SkillTool/SkillTool.ts:334`) | A skill body is content the model must actually read — overflow-to-disk would defeat the point |
| Auto-defer threshold | 10% of context window, `ENABLE_TOOL_SEARCH=auto:N` to override (`utils/toolSearch.ts:45-49`) | Below it, deferral costs a round-trip and saves little |
| Deferred-tool char estimate | `2.5` chars/token (`utils/toolSearch.ts:96-99`) | Tool schemas are JSON-dense — a lower ratio than prose |
| Deferred-tool announcement | bare name, no hint (`tools/ToolSearchTool/prompt.ts:110-117`) | Hints A/B-tested, no benefit, non-zero per-turn cost |
| Agent-list-in-description cost | ~10.2% of fleet cache-creation tokens (`tools/AgentTool/prompt.ts:52-55`) | The measured price of putting a volatile list in a cached prefix |

## Reusable pattern

```python
"""Progressive capability disclosure (stdlib only).

Level 1: an index line per capability, always in context, budgeted.
Level 2: the body, loaded only when the model invokes it.
Level 3: the capability's own directory, read on demand by file tools.

Applies to skills, tool schemas, playbooks, and reference docs alike.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

INDEX_CONTEXT_FRACTION = 0.01   # index is metadata, not content
CHARS_PER_TOKEN = 4
MAX_ENTRY_DESC_CHARS = 250      # discovery only; body loads on invoke
MIN_ENTRY_DESC_CHARS = 20       # below this, degrade to names-only


@dataclass(frozen=True)
class Skill:
    name: str                       # canonical = directory name
    description: str
    body: str                       # level 2, never in the index
    base_dir: Path | None           # level 3, read on demand
    source: str                     # 'bundled' | 'user' | 'project' | 'remote'
    model_invocable: bool = True    # may the agent self-invoke?
    user_invocable: bool = True     # may the human invoke?
    paths: tuple[str, ...] = ()     # non-empty => conditional
    extra: dict = field(default_factory=dict)   # anything beyond the known set


# ---- Level 1: the budgeted index -------------------------------------------

def render_index(skills: list[Skill], context_window_tokens: int) -> str:
    """Full entries if they fit; else trim third-party, protect first-party."""
    budget = int(context_window_tokens * CHARS_PER_TOKEN * INDEX_CONTEXT_FRACTION)
    line = lambda s, d: f"- {s.name}: {d}"
    desc = lambda s: s.description[:MAX_ENTRY_DESC_CHARS]

    full = [line(s, desc(s)) for s in skills]
    if sum(map(len, full)) + len(full) - 1 <= budget:
        return "\n".join(full)

    protected = [i for i, s in enumerate(skills) if s.source == "bundled"]
    used = sum(len(full[i]) + 1 for i in protected)
    rest = [i for i in range(len(skills)) if i not in set(protected)]
    if not rest:
        return "\n".join(full)

    overhead = sum(len(skills[i].name) + 4 for i in rest) + len(rest) - 1
    per_entry = (budget - used - overhead) // len(rest)

    if per_entry < MIN_ENTRY_DESC_CHARS:
        # Names-only is still discoverable: invoking loads the body.
        emit_metric("index_truncated", mode="names_only", n=len(skills))
        return "\n".join(
            full[i] if i in set(protected) else f"- {skills[i].name}"
            for i in range(len(skills))
        )
    emit_metric("index_truncated", mode="trimmed", n=len(rest))
    return "\n".join(
        full[i] if i in set(protected) else line(skills[i], desc(skills[i])[:per_entry])
        for i in range(len(skills))
    )


# ---- Loading: dedup by real path, first source wins -------------------------

def load(roots: list[tuple[Path, str]]) -> list[Skill]:
    """roots are ordered by precedence: policy, user, project, extra."""
    out, seen = [], set()
    for root, source in roots:
        for entry in sorted(root.iterdir() if root.is_dir() else []):
            manifest = entry / "SKILL.md"      # directory form only
            if not manifest.is_file():
                continue
            # realpath, not inode: inode is unreliable on virtual/NFS/ExFAT FS
            try:
                identity = os.path.realpath(manifest)
            except OSError:
                identity = None
            if identity is not None:
                if identity in seen:
                    continue                    # same file via symlink/overlap
                seen.add(identity)
            out.append(parse_skill(manifest, name=entry.name, source=source))
    return out


# ---- Conditional activation: path glob, evaluated on file touch ------------

def activate_for_paths(pending: dict[str, Skill], touched: list[str],
                       cwd: Path) -> list[str]:
    activated = []
    for name, skill in list(pending.items()):
        for p in touched:
            rel = os.path.relpath(p, cwd)
            if rel.startswith("..") or os.path.isabs(rel):
                continue                        # outside cwd can never match
            # NOT fnmatch: `*` must not cross `/`, and a bare name must match
            # at any depth. Substituting fnmatch diverges in BOTH directions —
            # see `matches_gitignore` in templates/agentkit/skills_loader.py.
            if any(matches_gitignore(rel, pat) for pat in skill.paths):
                activated.append(name)
                del pending[name]
                break
    return activated                            # caller adds these to the index


# ---- Invocation: typed refusals, then permission, then body ----------------

REFUSAL_EMPTY, REFUSAL_UNKNOWN, REFUSAL_MODEL_DISABLED = 1, 2, 4

def invoke(name: str, skills: dict[str, Skill]) -> dict:
    name = name.lstrip("/").strip()             # tolerate the slash form
    if not name:
        return {"ok": False, "code": REFUSAL_EMPTY, "error": "empty skill name"}
    skill = skills.get(name)
    if skill is None:
        return {"ok": False, "code": REFUSAL_UNKNOWN, "error": f"unknown skill: {name}"}
    if not skill.model_invocable:
        return {"ok": False, "code": REFUSAL_MODEL_DISABLED,
                "error": f"{name} cannot be invoked by the model"}

    decision = check_permission(skill)          # deny rules > allow rules > below
    if decision == "ask" and not prompt_user(skill):
        return {"ok": False, "code": 3, "error": "denied by user"}

    body = skill.body
    if skill.base_dir:                          # level 3 pointer
        body = f"Base directory for this skill: {skill.base_dir}\n\n{body}"
    if skill.source != "remote":                # untrusted sources: no expansion
        body = expand_templates(body, allowed_tools=skill.extra.get("allowed-tools", []))
    return {"ok": True, "name": name, "content": body}


# ---- Trust gate: allowlist, so NEW fields default to "ask" ------------------

SAFE_PROPERTIES = frozenset({
    "name", "description", "body", "base_dir", "source",
    "model_invocable", "user_invocable", "paths", "version",
})

def auto_allowable(skill: Skill) -> bool:
    """A property outside the allowlist with a meaningful value => ask.

    Allowlist, never blocklist: a capability added to the format next month
    requires permission until someone reviews and adds it here.
    """
    for key, value in skill.extra.items():
        if key in SAFE_PROPERTIES:
            continue
        if value in (None, "", [], {}, ()):     # declared but empty is harmless
            continue
        return False
    return True
```

Applied to tool schemas, the same ladder is: announce deferred tool **names**
only; expose one always-loaded search tool that returns full JSON schemas on
request; never defer the search tool itself, nor any tool whose description
carries a contract the model must honour before its first response.

## Pitfalls

- **Index growth eats the saving.** N skills × a verbose description is the
  tax you built this to avoid. Cap per-entry length, budget the total, and
  emit a metric when you truncate — silent degradation looks like "the model
  ignores my skill".
- **Writing `description` for humans.** It is the *entire* matching signal at
  level 1. It must state the trigger condition ("use when X"), not advertise
  the skill's quality.
- **Putting anything volatile in a cached prefix.** A capability list
  interpolated into a system prompt or tool description invalidates the prompt
  cache on every mutation, at a cost large enough to show up in fleet-level
  billing (constants table above). Inject it as a message instead.
- **Deferring the loader.** If the schema-fetch tool is itself deferred, the
  model cannot reach anything. Same for any tool whose prompt carries a
  turn-1 behavioural contract.
- **Dedup by name.** Symlinked and overlapping roots double-charge the budget
  and pick a winner non-deterministically; dedup by resolved real path, and
  never by inode.
- **Loading skills from dependency directories.** `node_modules/**/.claude/skills`
  is instruction injection from a supply-chain artifact. Filter by
  gitignore status, and treat the invocation-time permission prompt — not the
  filter — as the boundary.
- **Blocklisting unsafe frontmatter fields.** The field you add next month
  will not be on the list. Allowlist the safe ones so new capabilities
  default to asking.
- **Sanitising untrusted skill bodies for shell metacharacters.** Refuse
  expansion by source instead; you already know which source is remote.
- **Reaching for `fnmatch` when the docs say gitignore.** They differ in both
  directions: `fnmatch` lets `src/*.py` match `src/a/b.py` (too broad) and
  stops `foo.py` matching `pkg/foo.py` (too narrow). For a mechanism sold as a
  supply-chain filter, "too broad" is the one that matters.
- **Letting the model *mention* a skill instead of invoking it.** State it in
  the tool prompt as a blocking requirement, and give the model a way to
  detect that the body is already loaded so it does not re-invoke in a loop.
- **Skipping the "already running / already loaded" guard.** A skill whose
  body says "use the X skill" will re-enter itself forever without it.

## Checklist

- [ ] Level 1 (index) carries name + one trigger-shaped line, nothing more.
- [ ] Index is budgeted as a fraction of the context window, with staged
      degradation and a truncation metric.
- [ ] Level 2 (body) loads only on explicit invocation.
- [ ] Level 3 (directory) is announced by path in the body so files are read
      on demand, not preloaded.
- [ ] Skill identity is one canonical thing (directory name), shared by body
      and reference files.
- [ ] Frontmatter parsing is total: every field defaults, malformed values
      degrade rather than drop the skill.
- [ ] Human-invocable and model-invocable are separate flags.
- [ ] Candidates dedup by resolved real path; misses are cached too.
- [ ] Nested/lazy discovery is bounded (strictly below cwd), gitignore-gated,
      and deepest-wins.
- [ ] Conditional skills activate on a deterministic signal (path glob), and
      a match-all pattern collapses to "unconditional".
- [ ] Invocation returns typed refusal codes before doing permission work.
- [ ] Auto-allow is an allowlist of safe properties, so new fields ask.
- [ ] Untrusted sources are denied template/shell expansion by source check.
- [ ] Tool schemas use the same ladder above a measured threshold; the
      discovery tool and turn-1-contract tools are never deferred.
- [ ] No volatile list lives in a system prompt or tool description.
