# Case studies — how to distill the next agent

Each case study is one production agent codebase, reverse-engineered into the
shared reference library. The library grows incrementally: a new agent adds
one file here and enriches `references/` wherever its design differs.

## Distillation procedure

1. **Scope the codebase.** Locate the agent core (loop, tools, verifier) and
   count lines per file. Identify the 8–12 subsystems.
2. **Parallel deep-read.** One reader per subsystem, each producing
   structured findings: purpose, key mechanisms (with `file:line`), design
   decisions (+ rationale + tradeoff), constants (+ why this number), one
   generic reusable pattern, pitfalls, and mapping to the anatomy modules
   (`00-agent-anatomy.md`). Run a gap-fill pass over files no reader covered.
3. **Verify.** Every written claim with a `file:line` citation gets checked
   against the source by an adversarial second pass; wrong claims are fixed
   in place.
4. **Write the case study** (this directory): what the agent does, its
   architecture diagram, production constants table, the N design moves worth
   stealing, failure modes guarded against, known sharp edges, and what
   transfers to other domains.
5. **Merge into references.** Where the new agent *agrees* with existing
   references, add it as corroborating provenance. Where it *differs*, add a
   comparative subsection ("Agent X does Y instead, because Z") — differences
   between production agents are the most valuable content in this library.
6. **Update templates** only when a new pattern is strictly more general than
   the existing skeleton.

## Choosing the next agent

Prefer an agent that *disagrees* with what is already in the library. The
first two case studies were chosen to bracket one axis:

| | Articraft | Claude Code |
|---|---|---|
| Success is decided by | a compiler (mechanical) | a human (interactive) |
| Action space | a narrow domain SDK | an open-world shell + file surface |
| Load-bearing guard | the verifier | the permission system + sandbox |
| Context strategy | compaction on failure plateau | progressive disclosure of tools and skills |

Two agents that agree on everything produce one reference with two citations.
Two that disagree produce the comparative subsections that make the library
worth reading.

## Index

| # | Agent | Domain | Distilled |
|---|---|---|---|
| 01 | [Articraft](articraft.md) | text/image → articulated 3D assets (CadQuery → URDF) | 2026-08 · full (all 10 modules) |
| 02 | [Claude Code](claude-code.md) · [tool catalogue](claude-code-tool-catalog.md) | general-purpose coding agent, human in the loop | 2026-08 · full (~377k lines of non-UI source; references 11-15 + `templates/agentkit/`) |

A **partial** entry is legitimate and preferred over a shallow full one: distil the
subsystems you actually read and verified, and list the rest under *Coverage* in
the case study so the next pass knows where to start. Case study 02 landed as a
partial pass first (tools + skills) and was completed in a second pass; that is
the expected rhythm, not a failure.

## Two rules learned the hard way

**Scope by subsystem, not by line count.** Claude Code is ~512k lines, of which
~136k are terminal UI that teaches nothing about agent architecture. Deciding
what *not* to read is the first real decision; record it in the case study's
Coverage section so the exclusion is a choice rather than an accident.

**One canonical home per fact.** A striking number is quotable, which is
exactly why it spreads: the same measurement ends up restated in six
references and the library starts to read like it is selling something. Give
each load-bearing fact one place that states it with its citation, one entry
in the relevant constants table, and cross-references everywhere else. Case
studies are the exception — they are meant to be readable standalone.

**Verify citations mechanically — with anchors, not just line numbers.**
`tools/verify_citations.py` resolves every `file:line`, confirms the range
exists, and records a fingerprint of the cited lines in a committed lockfile.
Path-and-range checking alone has already caught real errors here (a miscount
from a grep that matched a doc comment; an ambiguous bare filename resolving to
the wrong directory) — but it silently passes a citation whose target has been
refactored away, which is the failure that actually matters one version later.
Run `--update` after writing, and `--source <newer-tree>` when a new version
ships: what comes back `DRIFTED` is your re-read list.
