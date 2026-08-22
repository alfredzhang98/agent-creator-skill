"""Acquire capability from the skills ecosystem instead of rebuilding it.

Before writing a tool, ask whether someone has already written the skill. The
open registry at ``skills.sh`` indexes thousands of them, and ``npx skills``
installs one in a line. A 3D agent should not re-derive USD conventions that
``nvidia/skills`` already documents.

This module ships the *judgement*, not the download. Searching is network and
installing is a subprocess; both belong to the host. Everything here is pure:
it turns a declared capability into queries, turns registry rows into a trust
verdict, turns a verdict into an exact command, and — after the host has run
that command — audits what actually landed on disk. ``UnconfiguredIndex``
refuses to fetch and prints the command to run by hand, which is the common
case when the person building the agent is not on the machine that runs it.

The distinction that drives every default here:

    **A library you call. A skill calls you.**

An installed dependency runs when your code invokes it. An installed skill's
prose is *loaded into the model's context as instructions*, carrying whatever
authority the surrounding prompt has. So the question at install time is not
"does this code have a CVE" — it is "am I willing to let this author write
instructions for my agent, in my agent's voice, with my agent's tools". That
reframing is why provenance and content are two separate gates (an author you
trust can still ship a body that asks for the world), why the content gate
reads the *prose* and not only the scripts, and why nothing is ever enabled by
reputation alone.

Two consequences the registry cannot help with:

* **There is no version pinning.** ``skills add`` takes a repo, ``skills
  update`` moves it to whatever HEAD says today. An author can rewrite your
  agent's instructions after you approved them. ``pin()`` hashes what you
  actually accepted so ``verify_pin()`` can notice; that is detection, not
  prevention, and it is the best the ecosystem currently allows.
* **Every installed skill is charged rent.** Its description sits in the index
  on *every* request for the life of the agent, whether or not it fires.
  Reference 11 sizes that budget; ``index_cost`` and ``budget_report`` spend
  against it, so six speculative installs cannot quietly evict the agent's own
  instructions.

Registry rows and skill bodies are both untrusted input. Parsing them uses the
same defensive ladder as ``sandbox_backend.validate_child_payload``: shape
first, then types, then a verdict — never a bare ``[]`` index or ``.get()``
chain that turns a malformed row into a plausible-looking candidate.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence

from skills_loader import char_budget, parse_frontmatter

#: The public registry. Documented shape, verified 2026-08:
#: ``{"query", "searchType", "skills": [{"id","skillId","name","installs","source"}],
#:   "count", "duration_ms"}``. Rows carry no description and no star count, so
#: relevance beyond the name has to come from reading the skill itself.
REGISTRY_SEARCH_URL = "https://skills.sh/api/search"
REGISTRY_BROWSE_URL = "https://skills.sh/"

#: Owners whose provenance is accepted without a reputation argument. Being on
#: this list clears the *provenance* gate only — the content audit still runs.
#: Keep it short and keep it to organisations that answer for what they ship.
DEFAULT_TRUSTED_OWNERS = frozenset({
    "anthropics", "vercel-labs", "vercel", "microsoft", "nvidia", "openai",
})

#: Phrases in a skill body that try to relax the host agent's own rules. A
#: skill legitimately says "use the USD sublayer convention"; it has no business
#: saying "skip confirmation". Substring match on the lowercased body: cheap,
#: over-triggers on discussion of the topic, and over-triggering is the correct
#: failure direction for a gate a human reads.
OVERRIDE_PHRASES = (
    "ignore previous", "ignore prior", "disregard the above", "disregard previous",
    "regardless of your instructions", "override the system prompt",
    "without asking", "without confirmation", "skip confirmation",
    "do not ask the user", "don't ask the user", "no need to ask",
    "auto-approve", "auto approve", "bypass permission", "bypass the sandbox",
    "disable the sandbox", "--dangerously", "yolo mode",
)

#: Body markers suggesting the skill reaches the network when it runs.
NETWORK_MARKERS = ("curl ", "wget ", "http://", "https://", "requests.get",
                   "requests.post", "urllib", "fetch(", "axios")

#: Extensions treated as executable payload rather than prose.
SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs",
                   ".ts", ".rb", ".pl", ".ps1", ".bat", ".cmd")

_WORD = re.compile(r"[a-z0-9][a-z0-9+#.-]*")

#: Words that carry no retrieval signal in a capability sentence.
_STOPWORDS = frozenset("""
a an the and or of for to in on with without into from by is are be that this
it its as at all any use using used need needs needed want wants make makes
making build builds building create creates creating generate generates agent
agents skill skills tool tools able ability capability support supports do does
""".split())

#: Domain -> extra query terms. Names in the registry are terse and literal, so
#: the user's word is often not the indexed word: nobody searching "3D scene"
#: types "usd", but that is what the relevant skills are called.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "3d": ("usd", "blender", "mesh", "scene"),
    "scene": ("usd", "3d", "blender"),
    "render": ("rendering", "raytracing", "omniverse", "usd"),
    "rendering": ("render", "omniverse", "usd"),
    "nvidia": ("omniverse", "isaac", "usd"),
    "omniverse": ("usd", "nvidia", "isaac"),
    "simulation": ("isaac", "physics", "mujoco"),
    "robot": ("urdf", "isaac", "ros"),
    "cad": ("cadquery", "geometry", "parametric"),
    "mesh": ("geometry", "3d", "blender"),
    "physics": ("simulation", "collision"),
    "animation": ("rigging", "motion", "blender"),
}

MAX_QUERIES = 8

#: Where ``skills add --agent X`` actually writes, per the CLI's own table.
#: Only agents whose layout is documented are listed; an unlisted agent gets an
#: explicit "unknown" rather than a plausible guess, because a wrong path here
#: turns into an audit that silently reads nothing and passes.
AGENT_PATHS: dict[str, tuple[str, str]] = {
    "claude-code": (".claude/skills", "~/.claude/skills"),
    "codex": (".codex/skills", "~/.codex/skills"),
    "cursor": (".cursor/skills", "~/.cursor/skills"),
    "opencode": (".opencode/skills", "~/.config/opencode/skills"),
    "amp": (".agents/skills", "~/.config/agents/skills"),
    "universal": (".agents/skills", "~/.config/agents/skills"),
}


def install_dir(agent: str, scope: str, skill_id: str) -> str:
    """Resolve where a skill lands, or "" when the layout is not documented."""
    paths = AGENT_PATHS.get(agent)
    if paths is None:
        return ""
    base = paths[1] if scope == "global" else paths[0]
    return os.path.join(os.path.expanduser(base), skill_id)


# ---------------------------------------------------------------------------
# 1. Capability -> queries
# ---------------------------------------------------------------------------

def queries_for(capability: str, *, extra: Sequence[str] = ()) -> list[str]:
    """Turn a declared capability into registry queries, best first.

    Deterministic term extraction plus a small synonym table — not semantic
    search. It exists because the registry matches on names, so the phrasing
    that reads well in a declaration ("an explorable 3D space") retrieves
    nothing while the term of art ("usd") retrieves the right shelf.
    """
    words = [w for w in _WORD.findall(capability.lower()) if w not in _STOPWORDS]
    seen: list[str] = []
    for w in words:
        if len(w) > 1 and w not in seen:
            seen.append(w)

    out: list[str] = []

    def push(q: str) -> None:
        q = q.strip()
        if q and q not in out:
            out.append(q)

    for term in extra:
        push(term.lower())
    if len(seen) >= 2:
        push(" ".join(seen[:2]))
    for w in seen[:2]:
        push(w)
    # Synonyms are emitted PAIRED with the word that suggested them, and they
    # go in before the long tail of literal words.
    #
    # Both halves of that are measured against the live registry, not guessed.
    # Paired, because a bare term of art retrieves its homonyms: "usd" returns
    # a stablecoin-transfer skill and a USD-futures trading skill before it
    # returns anything about Universal Scene Description, and "mesh" returns
    # service-mesh observability. "3d usd" and "3d mesh" put the right answer
    # first. Early, because appended last they were simply truncated away by
    # the cap, which left this table decorative.
    for w in seen:
        for syn in _SYNONYMS.get(w, ()):
            push(f"{w} {syn}" if syn != w else syn)
    for w in seen[2:]:
        push(w)
    return out[:MAX_QUERIES]


def search_url(query: str) -> str:
    """The exact URL to GET for one query.

    Exists so an agent holding nothing but a fetch tool can still run step
    zero. Building the URL is pure; performing the GET is not, and does not
    belong in this package.
    """
    from urllib.parse import quote
    return f"{REGISTRY_SEARCH_URL}?q={quote(query)}"


def search_urls(capability: str, *, extra: Sequence[str] = ()) -> list[str]:
    """Every URL to fetch for one declared capability, best query first."""
    return [search_url(q) for q in queries_for(capability, extra=extra)]


# ---------------------------------------------------------------------------
# 2. Registry rows (untrusted input)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One row from the registry, after validation."""

    id: str            # "nvidia/skills/omniverse-realtime-viewer"
    skill_id: str      # "omniverse-realtime-viewer"
    source: str        # "nvidia/skills"
    installs: int

    @property
    def owner(self) -> str:
        return self.source.split("/", 1)[0] if "/" in self.source else self.source

    @property
    def url(self) -> str:
        return REGISTRY_BROWSE_URL + self.id


def parse_search_response(payload: Any) -> list[Candidate]:
    """Validate a registry response into candidates, dropping malformed rows.

    Silence over exceptions: one bad row must not lose the good ones, and a
    changed schema must degrade to "found nothing" rather than to a candidate
    with an empty source that later renders as a shell command.
    """
    if not isinstance(payload, dict):
        return []
    rows = payload.get("skills")
    if not isinstance(rows, list):
        return []

    out: list[Candidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident, source = row.get("id"), row.get("source")
        skill_id = row.get("skillId") or row.get("name")
        if not (isinstance(ident, str) and isinstance(source, str)
                and isinstance(skill_id, str)):
            continue
        if not (ident.strip() and source.strip() and skill_id.strip()):
            continue
        # A source is the install target; anything with shell metacharacters or
        # traversal in it is refused outright rather than sanitised.
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+", source):
            continue
        if ".." in source or ".." in skill_id:
            continue
        installs = row.get("installs")
        out.append(Candidate(
            id=ident.strip(),
            skill_id=skill_id.strip(),
            source=source.strip(),
            installs=installs if isinstance(installs, int) and installs >= 0 else 0,
        ))
    return out


# ---------------------------------------------------------------------------
# 3. Gate one: provenance
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AcquisitionPolicy:
    """The trust bar, as data. Defaults are the safe values."""

    trusted_owners: frozenset[str] = DEFAULT_TRUSTED_OWNERS
    #: At or above this, an unknown owner clears provenance on reputation.
    min_installs: int = 1_000
    #: Below this, reputation is not an argument at all.
    review_floor: int = 100
    #: Cap on skills acquired for one agent. Every one costs index rent.
    max_skills: int = 6
    #: "project" (./<agent>/skills/, committed) or "global" (~, machine-wide).
    scope: str = "project"
    #: Allow a skill that ships executable payload. Off: prose only.
    allow_scripts: bool = False
    #: Refuse to enable anything whose content has not been hashed.
    require_pin: bool = True


OK, REVIEW, REFUSE = "ok", "review", "refuse"


@dataclass(frozen=True)
class Provenance:
    """Verdict on *who* published a skill. Never on what it says."""

    verdict: str                    # OK | REVIEW | REFUSE
    reasons: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.verdict == REFUSE


def assess(candidate: Candidate, policy: AcquisitionPolicy | None = None) -> Provenance:
    """Judge a candidate's origin.

    Fail-closed: an unrecognised owner with unremarkable adoption lands on
    REVIEW, not OK. OK here means "you need not argue about the author" — it
    never means "install it", because the content gate has not run yet.
    """
    policy = policy or AcquisitionPolicy()
    reasons: list[str] = []

    trusted = candidate.owner in policy.trusted_owners

    # A trusted owner settles *who wrote it*, not *whether anyone uses it*.
    # Big organisations host community and experimental repos under the same
    # name; a skill with single-digit adoption is unvetted no matter whose
    # namespace it sits in, so the floor still applies — it just lands on
    # REVIEW rather than REFUSE, because the author is answerable.
    if trusted:
        if candidate.installs < policy.review_floor:
            return Provenance(REVIEW, (
                f"{candidate.owner} is a trusted publisher, but "
                f"{candidate.installs} installs means nobody has exercised "
                f"this one — read it before enabling",))
        return Provenance(OK, (
            f"{candidate.owner} is a trusted publisher "
            f"({candidate.installs} installs)",))

    if candidate.installs < policy.review_floor:
        reasons.append(
            f"{candidate.installs} installs is below the floor of "
            f"{policy.review_floor}; nobody has vetted this for you"
        )
        return Provenance(REFUSE, tuple(reasons))

    if candidate.installs >= policy.min_installs:
        reasons.append(f"{candidate.installs} installs from {candidate.source}")
        return Provenance(OK, tuple(reasons))

    reasons.append(
        f"{candidate.installs} installs is under {policy.min_installs} and "
        f"{candidate.owner} is not a known publisher — read it before enabling"
    )
    return Provenance(REVIEW, tuple(reasons))


def merge(results: Iterable[Sequence[Candidate]],
          policy: AcquisitionPolicy | None = None) -> list[Candidate]:
    """Fold several queries' results into one ranked list.

    Step zero fires one query per phrasing, so the real input is N result
    lists, not one — and folding them is where the useful signal appears.
    A skill that surfaces near the top of *several different* phrasings is
    more likely to be the answer than one that wins a single query, because
    the queries disagree about wording and agree about it anyway. So the sort
    is (provenance, -distinct queries hit, best position), and a candidate's
    position is the best it achieved anywhere.

    Without this, callers hand-roll a dict and lose the agreement signal —
    which is exactly what the first draft of this module's own smoke test did.
    """
    policy = policy or AcquisitionPolicy()
    order = {OK: 0, REVIEW: 1}
    best: dict[str, tuple[int, int, Candidate]] = {}   # id -> (hits, pos, cand)
    for group in results:
        for position, c in enumerate(group):
            if assess(c, policy).blocked:
                continue
            hits, pos, _ = best.get(c.id, (0, position, c))
            best[c.id] = (hits + 1, min(pos, position), c)
    ordered = sorted(
        best.values(),
        key=lambda t: (order[assess(t[2], policy).verdict], -t[0], t[1], t[2].id),
    )
    return [t[2] for t in ordered]


def rank(candidates: Iterable[Candidate],
         policy: AcquisitionPolicy | None = None) -> list[Candidate]:
    """Drop refused candidates, demote the ones needing review, keep the rest
    in the order the registry returned them.

    The tempting sort is by install count, and it is wrong. The registry has
    already ordered these by match quality; re-sorting by popularity replaces
    *relevance* with *fame*, and the failure is quiet — searching "usd scene"
    surfaces a 2.3k-install iOS SceneKit skill above the 1.9k-install
    Omniverse viewer that is the actual answer. Adoption has already been
    spent, once, on the provenance verdict. Spending it again here just
    outvotes the only signal that knows what was asked.
    """
    policy = policy or AcquisitionPolicy()
    order = {OK: 0, REVIEW: 1}
    keep = []
    for position, c in enumerate(candidates):
        p = assess(c, policy)
        if not p.blocked:
            keep.append((order[p.verdict], position, c))
    keep.sort(key=lambda t: t[:2])
    return [t[2] for t in keep]


# ---------------------------------------------------------------------------
# 4. The command (built, never run)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InstallPlan:
    """Exactly what would be run, and where it would land."""

    candidate: Candidate
    argv: tuple[str, ...]
    scope: str
    target_dir: str      # "" when the agent's layout is undocumented
    provenance: Provenance

    @property
    def command(self) -> str:
        return " ".join(shlex.quote(a) for a in self.argv)


def plan_install(candidate: Candidate,
                 policy: AcquisitionPolicy | None = None,
                 *,
                 agent: str = "claude-code",
                 yes: bool = False) -> InstallPlan:
    """Build the install command. Raises if provenance already refused it.

    ``yes`` skips the CLI's own confirmation. Default off: the point of the
    prompt is that a human sees the source, and a plan generated by a model is
    exactly when that matters most.
    """
    policy = policy or AcquisitionPolicy()
    prov = assess(candidate, policy)
    if prov.blocked:
        raise ValueError(f"refused: {'; '.join(prov.reasons)}")

    argv = ["npx", "skills", "add", candidate.source,
            "--skill", candidate.skill_id, "--agent", agent]
    if policy.scope == "global":
        argv.append("-g")
    elif policy.scope != "project":
        raise ValueError(f"scope must be 'project' or 'global', got {policy.scope!r}")
    if yes:
        argv.append("-y")

    target = install_dir(agent, policy.scope, candidate.skill_id)
    return InstallPlan(candidate, tuple(argv), policy.scope, target, prov)


# ---------------------------------------------------------------------------
# 5. Gate two: content
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Audit:
    """What an installed skill actually asks for, read off disk."""

    path: str
    name: str = ""
    description: str = ""
    index_cost: int = 0        # chars charged on every request, forever
    body_cost: int = 0         # chars charged when it fires
    granted_tools: tuple[str, ...] = ()
    scripts: tuple[str, ...] = ()
    override_phrases: tuple[str, ...] = ()
    network: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def concerns(self) -> tuple[str, ...]:
        """Everything a human must sign off on, in severity order."""
        out: list[str] = []
        for phrase in self.override_phrases:
            out.append(f"body tries to relax host rules: {phrase!r}")
        if self.granted_tools:
            out.append("claims tools: " + ", ".join(self.granted_tools))
        if self.scripts:
            out.append(f"ships {len(self.scripts)} executable file(s): "
                       + ", ".join(self.scripts[:4]))
        if self.network:
            out.append("reaches the network: " + ", ".join(sorted(set(self.network))))
        return tuple(out)

    @property
    def clean(self) -> bool:
        return not self.concerns and not self.errors


def audit_skill(directory: str, *, max_bytes: int = 1_000_000) -> Audit:
    """Read an installed skill and report what accepting it would mean.

    Runs *after* the host installs and *before* the skill is enabled. Reading
    files is not executing them; nothing here imports, spawns, or evaluates
    what it finds.
    """
    md = os.path.join(directory, "SKILL.md")
    if not os.path.isfile(md):
        return Audit(path=directory, errors=("no SKILL.md",))
    try:
        with open(md, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(max_bytes)
    except OSError as exc:
        return Audit(path=directory, errors=(f"unreadable: {exc}",))

    meta, body = parse_frontmatter(text)
    name = str(meta.get("name") or os.path.basename(directory.rstrip("/")))
    desc = str(meta.get("description") or "")

    granted: list[str] = []
    for key in ("allowed-tools", "allowed_tools", "allowedTools", "tools"):
        raw = meta.get(key)
        if isinstance(raw, str):
            granted += [t.strip() for t in raw.split(",") if t.strip()]
        elif isinstance(raw, (list, tuple)):
            granted += [str(t).strip() for t in raw if str(t).strip()]

    low = body.lower()
    found_override = tuple(p for p in OVERRIDE_PHRASES if p in low)
    found_net = [m.strip() for m in NETWORK_MARKERS if m in low]

    scripts: list[str] = []
    for root, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
        for fn in filenames:
            if fn.lower().endswith(SCRIPT_SUFFIXES):
                rel = os.path.relpath(os.path.join(root, fn), directory)
                scripts.append(rel)
    scripts.sort()

    return Audit(
        path=directory,
        name=name,
        description=desc,
        index_cost=len(name) + len(desc) + 8,   # index line, roughly
        body_cost=len(body),
        granted_tools=tuple(dict.fromkeys(granted)),
        scripts=tuple(scripts),
        override_phrases=found_override,
        network=tuple(found_net),
    )


# ---------------------------------------------------------------------------
# 6. Pinning (detection, since the ecosystem offers no prevention)
# ---------------------------------------------------------------------------

def pin(directory: str) -> str:
    """Content hash of an installed skill: what you actually approved."""
    h = hashlib.sha256()
    entries: list[tuple[str, str]] = []
    for root, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
        for fn in filenames:
            full = os.path.join(root, fn)
            entries.append((os.path.relpath(full, directory).replace(os.sep, "/"), full))
    for rel, full in sorted(entries):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            with open(full, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return "sha256:" + h.hexdigest()


def verify_pin(directory: str, expected: str) -> bool:
    """True when the skill on disk is the one that was approved."""
    return bool(expected) and pin(directory) == expected


# ---------------------------------------------------------------------------
# 7. The record
# ---------------------------------------------------------------------------

@dataclass
class Acquisition:
    """One skill, from candidate to enabled — with why at every step."""

    candidate: Candidate
    plan: InstallPlan | None = None
    audit: Audit | None = None
    pinned: str = ""
    consented: bool = False
    notes: tuple[str, ...] = ()

    def blockers(self, policy: AcquisitionPolicy | None = None) -> tuple[str, ...]:
        """Why this may not be enabled yet. Empty means both gates passed."""
        policy = policy or AcquisitionPolicy()
        out: list[str] = []
        prov = assess(self.candidate, policy)
        if prov.blocked:
            out.append("provenance refused: " + "; ".join(prov.reasons))
        if self.audit is None:
            out.append("not audited")
        else:
            out.extend(self.audit.errors)
            if self.audit.override_phrases:
                out.append("body attempts to relax host rules")
            if self.audit.scripts and not policy.allow_scripts:
                out.append(f"ships {len(self.audit.scripts)} script(s) and "
                           "policy.allow_scripts is off")
        if policy.require_pin and not self.pinned:
            out.append("not pinned")
        if not self.consented:
            out.append("no recorded consent")
        return tuple(out)

    def enabled(self, policy: AcquisitionPolicy | None = None) -> bool:
        return not self.blockers(policy)


def budget_report(acquisitions: Sequence[Acquisition],
                  context_window_tokens: int | None = None,
                  policy: AcquisitionPolicy | None = None) -> dict[str, Any]:
    """Index rent for a set of skills against reference 11's budget."""
    policy = policy or AcquisitionPolicy()
    limit = char_budget(context_window_tokens)
    spent = sum(a.audit.index_cost for a in acquisitions if a.audit)
    return {
        "skills": len(acquisitions),
        "max_skills": policy.max_skills,
        "over_count": len(acquisitions) > policy.max_skills,
        "index_chars": spent,
        "index_budget": limit,
        "over_budget": spent > limit,
    }


# ---------------------------------------------------------------------------
# 8. The seam: search and install belong to the host
# ---------------------------------------------------------------------------

class SkillIndex(Protocol):
    """What a host implements to make acquisition automatic.

    ``search`` needs the network, ``install`` needs a subprocess. Neither is in
    this package, on purpose: no module an agent imports may spawn a process.
    """

    def search(self, query: str, *, owner: str | None = None) -> list[Candidate]: ...

    def install(self, plan: InstallPlan) -> str:
        """Run ``plan.argv`` and return the directory the skill landed in."""


@dataclass
class UnconfiguredIndex:
    """The default: refuses, and hands back the commands to run by hand.

    Not a stub. Building an agent for a machine you are not sitting at is the
    normal case, and a copy-pasteable command is the right output for it.
    """

    reason: str = "no SkillIndex is wired in (search needs network, install needs a subprocess)"

    def search(self, query: str, *, owner: str | None = None) -> list[Candidate]:
        raise UnconfiguredSkillIndex(
            f"{self.reason}. Search by hand:\n"
            f"    npx skills find {query}"
            + (f" --owner {owner}" if owner else "")
            + f"\n  or browse {REGISTRY_BROWSE_URL}"
        )

    def install(self, plan: InstallPlan) -> str:
        raise UnconfiguredSkillIndex(
            f"{self.reason}. Install by hand:\n    {plan.command}"
        )


class UnconfiguredSkillIndex(RuntimeError):
    """Raised by :class:`UnconfiguredIndex`; the message is the instructions."""


def manual_script(plans: Sequence[InstallPlan]) -> str:
    """A shell script the user can paste on the machine that runs the agent."""
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    for p in plans:
        lines.append(f"# {p.candidate.id} — {p.candidate.installs} installs")
        for reason in p.provenance.reasons:
            lines.append(f"#   {reason}")
        lines.append(p.command)
        lines.append("")
    return "\n".join(lines)
