# Feedback to the agent-creator skill

From a real build. Each item is an incident, its cost, what the skill says
today, and the change. Priority order.

Every claim below was checked against the tree at `c19e40a` before being
written down. Two came back **worse** than reported, one came back **half
already fixed**, and the last section is what that check turned up that
nobody had flagged.

---

## 1. There is no category for a failure the model cannot author a fix for

**What happened.** A failure entered the feedback loop that no model action
could clear. The loop treated it like every other failure — summarise it,
pick a playbook, hand it back, try again.

**What it cost.** Every turn after the first was spend against a wall.

**What the skill says today.** Reference 03 is built end to end on "a failure
is feedback to the model". It has a rich notion of *attribution* — the
advisory tier reports only what the model's own edit broke — but attribution
answers *should we mention this to the model*, never *can the model do
anything about it*. `_failure_sort_key` sorts by causal dependency and has an
`unknown = 90` bucket, and `unknown` still goes back into the loop like the
rest. Nothing in the five invariants covers it.

**The fix.** A sixth invariant, and it belongs with the mechanical ones:

> **6. A failure with no author terminates the run.** Some failures are not
> the model's to fix and not fixable by retrying: a model ID that does not
> exist, a missing binary, an exhausted quota, a revoked key, a file the
> sandbox will never be allowed to see. They have no author, so there is no
> edit that clears them. Classify authorship *before* choosing a playbook —
> authored failures iterate, authorless failures exit with the operator named
> as the party who must act.

The classification is cheap and mostly mechanical: HTTP 401/403/404, `ENOENT`
on a binary, quota and entitlement errors, sandbox policy refusals. Everything
unclassified stays authored, because fail-closed here means "keep trying",
which is the current behaviour and therefore safe to leave as the default.

---

## 2. Repeat detection escalates the wording and never changes the loop

**What happened.** Implemented exactly what the skill describes: track a
consecutive-failure streak, and at `streak >= 3` switch to stronger advice.
Ran to 40 turns anyway.

**What it cost.** 37 turns of spend past the point the outcome was decided.

**What the skill says today — and this is worse than reported.** The skill
does not merely under-specify this. It documents *two* streaks and gives them
opposite treatment, without ever noting the asymmetry:

| Streak | Reference | What happens at the limit |
|---|---|---|
| No-action (empty turn) | `01`, line 21 | **Aborts the run** at 3, gated on the escalation having been delivered |
| Consecutive verify failure | `01` line 25, `03` line 27 | Rewords the advice; raises reasoning effort. **Never exits.** |

So the skill already contains the mechanism — an abort gated on "the warning
was actually delivered first", which is the careful version of exactly what is
missing — and applies it only to the streak where a turn produced *nothing*.
The streak where a turn produced something wrong, repeatedly, only gets a
better-phrased suggestion.

Anyone implementing from the text will build what was built here, because the
text describes escalating advice and calls that the answer.

**The fix.** State that a streak is a loop-control signal, and give the
failure streak the same three rungs the no-action streak already has:

```
streak 1-2   reword; raise effort                     (current behaviour)
streak 3     inject the escalation, and mark it delivered
streak 4     exit, with the streak signature in the exit reason
```

Two properties worth carrying over from the no-action ladder: the exit is
gated on the escalation having been delivered, so spend is never written off
without a final warning; and *any* productive turn resets both. Add the
signature check as a separate, faster trip: **identical signature twice in a
row is not a streak, it is a stall** — nothing changed between attempts, so a
third attempt has no new information to work with.

---

## 3. The configuration users actually run is the one with the least coverage

**What happened.** Fixtures chose absolute temp directories and empty caches
for isolation. Production runs relative paths and a warm cache. The tests
passed on a configuration nobody runs.

**What it cost.** A bug class that testing structurally could not reach.

**What the skill says today.** Nothing. Reference 08 covers staging and
resume, reference 03 covers verification tiers, and neither says a word about
fixtures diverging from the deployed default.

**The fix.** A short section in reference 03, next to the tiers:

> Every isolation choice a fixture makes is a difference from production, and
> differences are where bugs live. List them explicitly — temp dir vs working
> dir, cold cache vs warm, fresh state vs resumed, absolute vs relative paths,
> no network vs restricted — and for each one either justify it or run one
> test the other way. The default configuration deserves at least one test that
> is not isolated from it.

Cheap and concrete. This tree is not exempt: its own suite uses a temporary
directory almost everywhere.

---

## 4. "Compute the fix suggestion" is demonstrated once and never stated as a rule

**What happened.** The most useful thing the verifier produced was not the
finding — it was the *computed* repair: the exact value, the exact line, the
exact replacement. That behaviour was copied out of a single Articraft
example rather than followed from a rule.

**What it cost.** It nearly did not get built. Nothing marked it as load-bearing.

**What the skill says today.** `grep` for "fix suggestion", "suggested fix",
"propose a fix" across all sixteen references: no hits. The Articraft playbook
material in reference 03 shows sequenced advice, which is a *category* of
response, not a computed one.

**The fix.** Promote it to a rule in reference 03:

> A finding states what is wrong. A good finding states what to write instead.
> Where the check knows the correct value — a bound it compared against, a
> name it looked up, a unit it converted — emit it. "Mass 0.0 is invalid" costs
> a turn to act on; "mass 0.0 is invalid; the bounding volume implies 0.42 kg
> at the declared density" costs none. Never emit a suggestion the check did
> not actually compute: a guessed fix is worse than no fix, because it will be
> applied.

---

## 5. There is a budget cap but no rule about verifying preconditions before spending

**What happened.** An agent was built against `gpt-5.6`. No such model exists;
the account had `gpt-5.6-luna`, `gpt-5.6-sol` and `gpt-5.6-terra`. One free
`GET /v1/models` would have caught it before the first paid call.

**What it cost.** A run that could not have succeeded, paid for in full.

**What the skill says today — half of this is now fixed.** As of `v0.8.0` the
specific hole is closed: `preflight.py` resolves the model ID against the live
catalogue, refuses a family name rather than picking a variant, and checks the
key, the declared capability limits and a one-token ping before turn one.
SKILL.md carries the `curl` and the declaration block carries a `Model` line.

The **general rule is still missing.** Reference 07 is about metering and caps
— spend is counted, and it is stopped when it exceeds a number. Nothing says
that a cheap check which can *prove the run cannot succeed* must run before any
expensive one. Model resolution was one instance; a missing binary, an
unwritable output path, an unreachable service and an expired credential are
the same shape and are not covered.

**The fix.** Add to reference 07, as the section before the caps:

> A budget stops a run that is going badly. A precondition stops a run that
> was never going to work. They are not the same control and the second one is
> free. Before the first paid call, assert everything cheap that the run
> depends on: credentials present, model ID resolves, binaries on PATH, output
> path writable, dependent services reachable. Order them by (cost ascending,
> probability of failure descending). Every one of these failures is authorless
> in the sense of invariant 6 — which is why they belong here, before spending
> starts, rather than in the loop that assumes a model can fix things.

---

## 6. Three smaller ones

Grouped for length, not because they are minor — **6b and 6c are arguably
above item 3.**

### 6a. The sandbox chapter never warns about path re-resolution

**What happened.** A path was resolved once by the parent and again by the
child, against a different working directory. The second resolution silently
won.

**What it cost.** A confinement boundary that read as enforced and was not.

**What the skill says today.** Reference 04 mentions `cwd` exactly once, and
descriptively: the probe runner pins cwd to the repo root. It is stated as an
implementation detail of that example, not as a rule, and nothing warns that a
path crossing the boundary gets interpreted twice.

**The fix.** A pitfall in reference 04:

> Any path that crosses the sandbox boundary is resolved twice — once by you,
> once by whatever runs inside — and the two resolutions have different
> working directories, different symlink views and possibly different mount
> namespaces. Resolve to an absolute real path on the parent side, validate
> *that*, and pass only the resolved form. A relative path handed to a child is
> not a path, it is a request for the child to pick one. The same trap applies
> to arguments the child re-parses: shell metacharacters, `@file` expansions,
> config paths read from the payload.

### 6b. Executable documentation is absent, and it is the highest-yield practice here

**What happened.** Making the test suite actually compile and run the examples
from the documentation was the single most valuable thing added to the build.

**What the skill says today.** `grep` for "executable doc", "doctest",
"compile the example", "test the doc" across all sixteen references: no hits.

**Independent corroboration, from this tree, two commits before this file was
written.** Reference 16's own "Reusable pattern" block raised `AttributeError`
as written — `rank(index.search(q) for q in queries)` passes a generator of
lists, so the first attribute touched is `list.owner`. It had shipped. Nobody
noticed, because it lived in prose and prose does not run. It is now executed
by `t_the_documented_pattern_actually_runs`.

That is the same defect class as the review's original headline finding: *the
check stopped before the thing it was checking*. It recurred in a repository
whose regression suite exists specifically to prevent it. That is the argument
for making it a rule rather than a habit.

**The fix.** Reference 03, promoted to a named practice:

> Documentation is untested code with better formatting. Every code block a
> reader might paste should be executed by the suite — imported, run,
> asserted. Not "does it parse": does it produce the documented result. A
> broken example is worse than a missing one, because it is followed.

### 6c. Two gates disagreeing is a finding, not a cost trade-off

**What happened.** A cheap gate and an expensive gate returned different
verdicts on the same input. That disagreement was the most informative event of
the session — it localised the bug faster than either gate alone.

**What the skill says today.** Reference 03 describes exactly this pairing and
frames it purely as an optimisation: cheap structural gates run first "with
early return so expensive geometry QC never runs on an invalid model". Correct
and worth doing — but the early return means the expensive gate is *skipped*,
so a disagreement can never be observed. The design does not just fail to use
the signal; it discards it.

**The fix.** In reference 03, after the ordering guidance:

> Ordering gates cheap-to-expensive saves money. It also destroys the signal
> that a cheap gate passing and an expensive gate failing would have given you,
> because the expensive one never runs. When they *do* both run, treat
> disagreement as a first-class finding: cheap-pass/expensive-fail means the
> cheap gate has a hole worth closing, and cheap-fail/expensive-pass means it
> has a false positive that is costing every run. Log both verdicts even when
> the first short-circuits the second, and sample a small fraction of runs with
> the early return disabled so the comparison exists at all.

---

## What the check turned up that was not on the list

Found while verifying the six above, in the tree rather than in the session.

### A. SKILL.md mandates a second cost axis that `cost_meter.py` cannot meter

SKILL.md, in the "wire these in every time" list, says: *if the domain has a
second cost axis (GPU time, API quota, physical actuation), cap that too and
estimate it before the call.* The worked example's declaration block shows
`Budget: tokens + GPU-seconds, estimated pre-call`.

`templates/cost_meter.py` has `pricing_for`, `calculate_cost`, `Breakdown`,
`CostMeter`, `resolve_budget` — all of them USD over token counts. There is no
second axis, no unit that is not a token, no pre-call estimator. `grep` for
"gpu", "quota", "units", "second axis": no hits.

So the skill instructs a capability its own library does not have, and its own
worked example declares it. Anyone following both will discover the gap at
implementation time. Either generalise `CostMeter` to N named axes with a
per-axis cap and a pre-call estimate hook, or say plainly in SKILL.md that the
second axis is the builder's to implement and give the shape it should take.

### B. Nothing covers the verifier itself being wrong

Invariant 1 says success is verified, never self-reported. The verifier is
therefore the authority — and the skill has no guidance for the case where the
authority is broken. This is not hypothetical: three real bugs were found in
this tree's own verifier once a second source became available to check it
against, and all thirty-seven "broken" citations it reported were its own
defects rather than wrong claims.

A verifier that fails open is invisible; every run passes and nothing says
why. Worth a short section: verifiers need a known-bad fixture that must fail,
their unavailability must be distinguishable from their success (`CheckResult`
already does this — say so), and a verifier that has never rejected anything
is a verifier nobody has tested.

### C. Streaks are within-run only

Item 2 is about a streak inside one run. Nothing addresses the same failure
recurring across runs — which is the more expensive version, because each run
starts with no memory of the last. Reference 14 defines memory by
non-derivability, and "this approach failed the last four times" is exactly
non-derivable from the code. The connection is not made anywhere.

### D. The standing one, restated

None of the agentkit has been run against a live model, and the library still
rests on two distilled agents. Item 2 above is a good illustration of what that
costs: the asymmetry between the two streak ladders survived a full independent
review and a 257-assertion suite, because nothing in either exercises a loop
that keeps failing in the same way.
