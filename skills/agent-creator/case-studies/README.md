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

## Index

| # | Agent | Domain | Distilled |
|---|---|---|---|
| 01 | [Articraft](articraft.md) | text/image → articulated 3D assets (CadQuery → URDF) | 2026-08 |
