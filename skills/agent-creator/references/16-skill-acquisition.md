# 16 · Skill acquisition — buy capability before you build it

Companion module: `templates/agentkit/skill_acquisition.py`.

## Why this module exists

Every reference before this one helps you *build* a capability. This one asks
the question that should come first: has someone already built it?

The open skills ecosystem indexes thousands of skills, installable in one
line. An agent that renders 3D scenes should not re-derive USD layer
conventions that `nvidia/skills` already documents; an agent that inspects
Blender state should not rediscover what a 4,000-install skill already
encodes. Writing the tool yourself is the expensive path, and it is the
default only because nobody asked the cheaper question.

But acquisition is not `pip install`, and treating it that way is the failure
mode this module exists to prevent:

> **A library you call. A skill calls you.**

An installed dependency runs when your code invokes it. An installed skill's
prose is loaded into the model's context *as instructions*, carrying whatever
authority the surrounding system prompt has. It can tell your agent to skip
confirmation, to prefer its own tool, to treat a path as safe. Nothing
executes and the agent's behaviour changes anyway.

So the install-time question is not "does this package have a CVE". It is:

> Am I willing to let this author write instructions for my agent, in my
> agent's voice, with my agent's tools?

That question has two independent halves, and they need two independent gates.

## How the ecosystem implements it

Verified against the live registry and the published CLI documentation on
2026-08-22. Unlike references 00–15, these are not `file:line` claims against
a pinned tree — the source is a hosted service, so re-verify rather than trust
the transcription.

| Piece | Reality |
|---|---|
| Search API | `GET https://skills.sh/api/search?q=<query>` → `{query, searchType, skills[], count, duration_ms}` |
| Row shape | `{id, skillId, name, installs, source}` — **no description, no stars, no version** |
| Search kind | fuzzy, over names; results arrive ordered by match quality |
| Install | `npx skills add <owner/repo> --skill <name> --agent <agent> [-g] [-y]` |
| Discovery | `npx skills find <query> [--owner <owner>]` |
| Lifecycle | `skills list`, `skills remove`, `skills update` |
| Scope | project → `.claude/skills/`; global → `~/.claude/skills/` (per-agent table) |
| Layout in repo | `skills/<name>/SKILL.md`, up to three levels deep |

Two absences matter more than anything present.

**There is no version pinning.** `add` takes a repo and resolves HEAD. `update`
moves to whatever HEAD says today. An author can rewrite the instructions you
approved, and nothing in the protocol tells you. Prevention is not available;
detection is, and that is what `pin()` / `verify_pin()` are for.

**Rows carry no description.** Relevance beyond the name has to come from
reading the skill. Any ranking you compute from a search response is ranking
names and popularity — which is precisely why it must not be trusted to make
the decision.

## Design decisions

**Two gates, neither substituting for the other.** `assess()` judges the
author; `audit_skill()` judges the text. An author you trust can still ship a
body that asks for the world, and an unknown author can ship something
perfectly ordinary. Collapsing them into one score is how "5,000 installs"
starts standing in for "I read it".

**Provenance clears the argument, never the install.** `OK` from `assess()`
means "you need not debate the author". It never means "proceed" — nothing is
enabled until `Acquisition.blockers()` is empty, which requires an audit, a
pin, and recorded consent on top.

**A trusted owner does not waive the adoption floor.** Large organisations
host community and experimental repos in the same namespace. A skill with
single-digit installs is unexercised no matter whose name is above it, so it
lands on `REVIEW` — softened from `REFUSE` because the author is answerable,
not skipped.

**The audit reads the prose.** `OVERRIDE_PHRASES` scans the body for language
that relaxes the host's rules — "skip confirmation", "auto-approve", "ignore
previous". It over-triggers on skills that legitimately *discuss* permissions,
and that is the correct failure direction for a gate a human reads. A skill
that ships no code at all can still be the dangerous one.

**Agreement across phrasings beats any single query.** Step zero fires one
query per phrasing, so the input is N result lists. `merge()` sorts by
(provenance, how many distinct queries surfaced it, best position it reached).
Queries disagree about wording; when they agree about a skill anyway, that is
the strongest relevance signal available from an index that ships no
descriptions.

**Search and install belong to the host.** Searching needs the network;
installing needs a subprocess. No module in this package spawns a process, so
`SkillIndex` is a `Protocol` and `UnconfiguredIndex` is the default — the same
seam as `SandboxBackend` in reference 04.

**The default refusal is a deliverable.** `UnconfiguredIndex` does not just
fail; its exception message *is* the command to run by hand, and
`manual_script()` emits a pasteable script with the provenance reasons as
comments. Designing an agent for a machine you are not sitting at is the
normal case, not the degraded one.

## Constants that matter

| Constant | Value | Why |
|---|---|---|
| `min_installs` | 1,000 | Above this, adoption is an argument on its own |
| `review_floor` | 100 | Below this, adoption is not an argument at all |
| `max_skills` | 6 | Index rent is paid on every request, forever |
| `MAX_QUERIES` | 8 | Recall against a fuzzy name index, without a flood |
| `scope` | `project` | Committed with the agent, reviewable in diff; global is invisible to the next reader |
| `allow_scripts` | `False` | Prose is the norm; executable payload is an exception you opt into |
| `require_pin` | `True` | The only defence against an author editing what you approved |
| `yes` | `False` | The CLI's own prompt is the last human checkpoint, and a plan written by a model is exactly when it matters |

## Reusable pattern

```python
from skill_acquisition import (
    AcquisitionPolicy, Acquisition, audit_skill, manual_script, merge,
    parse_search_response, pin, plan_install, rank, search_urls,
)

policy = AcquisitionPolicy(scope="project")

# 1+2. Capability -> queries -> URLs. Terms of art, paired. Pure: the GET is
# yours to make, with a fetch tool, curl, or a wired SkillIndex.
groups = []
for url in search_urls("an explorable 3D space rendered with NVIDIA"):
    groups.append(rank(parse_search_response(get_json(url))))

# 3. Fold the queries together. Agreement across phrasings is the signal.
candidates = merge(groups)

# 4. Plan, do not run.
plans = [plan_install(c, policy) for c in candidates[:3]]
print(manual_script(plans))        # works with no backend wired in at all

# 5. After the host installs: audit before enabling.
acq = Acquisition(candidates[0], plan=plans[0])
acq.audit  = audit_skill(plans[0].target_dir)
acq.pinned = pin(plans[0].target_dir)
acq.consented = human_said_yes(acq.audit.concerns)

if acq.enabled(policy):
    enable(acq)
else:
    report(acq.blockers(policy))   # every reason, in severity order
```

Three defects this shape is built around, each found by the first run against
the live registry rather than reasoned about in advance:

1. **Popularity outvoted relevance.** Sorting candidates by install count put
   a 2,377-install iOS SceneKit skill above the 1,968-install Omniverse viewer
   that was the actual answer. The registry had already ordered by match
   quality; the sort discarded the only signal that knew what was asked.
   `rank()` now preserves registry order and uses provenance only to demote.
2. **A trusted owner auto-approved a 1-install community skill**, because the
   owner check short-circuited the floor.
3. **The install path was an f-string.** `.claude-code/skills/` looked right
   and does not exist; the real path is `.claude/skills/`. A wrong path makes
   the audit read an empty directory and report no concerns — the gate fails
   *open*, which is the worst way for a gate to be wrong.

## Pitfalls

- **Ranking by installs.** Fame is not relevance. Adoption has already been
  spent once, on the provenance verdict; spending it again outvotes the search.
- **Bare terms of art as queries.** The index is fuzzy and over names, so
  homonyms dominate: `usd` returns a stablecoin-transfer skill and a
  USD-futures trading skill before Universal Scene Description; `mesh` returns
  service-mesh observability. Pair the term with its domain word — `3d usd`,
  `3d mesh` — and the right answer comes first.
- **Auditing only the scripts.** The body is the payload. A prose-only skill
  that says "auto-approve writes" is more dangerous than one shipping a shell
  script you can read.
- **`-y` in a generated command.** It removes the one checkpoint where a human
  sees the source, at exactly the moment a model chose it.
- **Installing globally by default.** `-g` puts a skill in every project on the
  machine, invisible to anyone reading this agent's repo. Prefer project scope
  so acquisition shows up in a diff.
- **Trusting `skills update`.** It moves to today's HEAD. Re-verify the pin
  after any update, or your reviewed instructions are gone with no signal.
- **Speculative installs.** Six "might be useful" skills spend the index budget
  that reference 11 sizes, on every request, whether or not they ever fire.
- **Treating "no skill found" as failure.** It is the common case. The question
  is cheap to ask and the answer is usually "build it" — the point is to have
  asked, not to always find something.

## Checklist

- [ ] Before writing a tool, the capability was searched for. If nothing fit,
      say so in the declaration — "no skill found for X" is a real answer.
- [ ] Registry rows are validated, not indexed into. A `source` becomes argv.
- [ ] Candidates are ranked by relevance; installs feed the verdict only.
- [ ] Provenance and content are separate gates, and both ran.
- [ ] The audit read the body, not just the file listing.
- [ ] Nothing is enabled without a pin and a recorded consent.
- [ ] Scope is `project` unless there is a stated reason for `global`.
- [ ] Acquired skills are counted against reference 11's index budget.
- [ ] If no backend is wired in, the output is a pasteable script, not an error.
- [ ] Pins are re-verified after any `skills update`.
