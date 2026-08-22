# 03. Evaluator & Verifier (Compile Feedback)

**Maps to:** Evaluator/Verifier · Guardrails · State/Context · Executor · **Distilled from:** Articraft `agent/feedback.py`, `agent/compiler.py`, `agent/harness_compile.py` · Claude Code 2.1.88 `src/services/diagnosticTracking.ts`, `src/services/lsp/`

## Why this module exists

An agent that writes code (or any executable artifact) needs a verifier that answers "is it correct?" with more than a raw traceback. Raw check output is unusable as LLM feedback: it mixes fatal errors with heuristic noise, presents N co-equal failures (the model patches the easiest one), repeats itself verbatim across attempts, and leaks unbounded tracebacks into the context window. This module executes the candidate, runs two layers of checks (agent-authored tests plus a verifier-owned safety net), and compresses every outcome into a typed, deduplicated signal bundle with exactly one "Primary issue" chosen by causal priority, severity-tiered sections, and per-failure-family playbooks that escalate when the agent loops. A harness-side wrapper makes verification mandatory: it tracks edit revisions, caches fresh results, detects exact-repeat failures, and injects "verify before concluding" reminders.

## How Articraft implements it

### Typed signal vocabulary
Every finding becomes a `CompileSignal` with severity in {failure, warning, note}, source in {compiler, tests, harness}, group in {build, qc, design, hygiene}, a stable machine code, a kind slug, and an explicit `blocking` bool (`agent/models.py:37-62`). 23 known finding classes are pre-declared as frozen `SignalSpec` module constants (`agent/feedback.py:74-81`, `agent/feedback.py:142-309`); free-text warnings are matched to specs by substring needles in `COMPILER_WARNING_RULES` (`agent/feedback.py:90-140`). Only failure-severity specs block. `dedupe_key` is a sha1 over all signal fields (`agent/feedback.py:546-582`), used as the dict key at bundle assembly so identical findings collapse.

### Text-to-signal classification
Three classifiers turn strings into typed signals, each ending in a generic fallback spec so unknown text is never dropped. `_warning_signal_from_text` parses headline/details then scans needle rules (`agent/feedback.py:624-658`). `_iter_test_failures` dispatches on check name and detail substrings — structural policies map to verifier-owned blocking specs, contract failures get regex-extracted magnitudes rendered human-readable via `_format_distance_summary` (`agent/feedback.py:673-798`, `agent/feedback.py:614-621`). `_warning_signal_from_test_text` runs an ordered chain of 7 classifier functions, downgrading low-risk heuristic findings to notes (classifier chain at `agent/feedback.py:801-917`, dispatcher at `agent/feedback.py:920-934`).

### Bundle assembly with exception-suppression precedence
`build_compile_signal_bundle` collects signals keyed by `dedupe_key`: compiler warnings (skipping text duplicated in test warnings), classified test warnings, allowance notes, then test failures (`agent/feedback.py:1100-1154`). The raw Python exception becomes a `COMPILE_RUNTIME_FAILURE` signal **only when zero structured test failures exist** (`agent/feedback.py:1128-1134`) — the exception wrapping a failed test report is redundant with the report. `compile_signal_bundle_from_exception` prefers a pre-built bundle riding on the exception, else rebuilds from attached `warnings`/`test_report` attributes (`agent/feedback.py:1142-1154`), so subprocess-serialized failures render identically to in-process ones.

### Primary-issue selection: causal priority ladder
`_failure_sort_key` assigns each failure an integer priority ordered by causal dependency: runtime error = 0 (script must execute before anything else matters), structural policies = 1, verifier-owned global QC = 2, stale authored contracts = 3-4, authored QC = 5-6, generic = 7, unknown = 90; ties break on `(kind, summary)` (`agent/feedback.py:1022-1040`). The head element's kind maps to one canonical "Primary issue: ..." sentence (`agent/feedback.py:1050-1079`) that heads the summary and selects the playbook.

### LLM-facing rendering
`render_compile_signals` emits pseudo-XML: `<summary>` ("status=X failures=N warnings=N notes=N" + Primary issue line), `<failures>` ("Failures (blocking):"), `<warnings>` ("Warnings (non-blocking):"), `<notes>`, `<response_rules>` ("Suggested next steps:") (`agent/feedback.py:1176-1248`, signal-line helpers at `agent/feedback.py:1157-1174`). Failures render in priority order; each signal is `- SEVERITY [kind] summary` with details indented 2 spaces. `repeated=True` appends "This failure matches the previous compile attempt."; `failure_streak >= 3` appends "This is compile failure N in a row." (`agent/feedback.py:1198-1201`). Notes are suppressed on otherwise-clean output (`agent/feedback.py:1222`).

### Playbooks with streak escalation
`_response_rules_for_failures` selects a hand-written 2-7 bullet playbook keyed on the primary failure's kind — e.g. runtime error → "fix the runtime error first, geometry repair is blocked"; structural policy → "fix the part tree, do not tune geometry"; QC families → sequenced advice ending in "scope any allowance to the exact reported pair with a reason plus a proof check" (`agent/feedback.py:1251-1390`). If warnings coexist, a "warnings are design evidence" bullet appears even on success (`agent/feedback.py:1288-1293`). Repeated QC failures add "a short diagnostic probe is likely more informative than another small tweak" (`agent/feedback.py:1375-1378`); streak >= 3 switches to family-specific loop-breaking advice — audit contracts, probe before patching (`agent/feedback.py:1379-1389`).

### Exception distillation
`_runtime_failure_signal` builds "ExcType: first-detail-line" summaries (`agent/feedback.py:1002-1015`); details are truncated at embedded tracebacks, deduplicated, capped at 40 lines with an elision marker, and known-confusing errors get canned domain hints appended (helpers at `agent/feedback.py:324-464`, hint tables at `agent/feedback.py:15-25`). Location extraction prefers the remote traceback shipped from the subprocess and deliberately skips local frames when the exception is the wrapper RuntimeError (`agent/feedback.py:467-500`). `_sanitize_display_path` rewrites paths repo-relative or `.../<last-3-components>` (`agent/feedback.py:335-370`) — portable, token-cheap, and stable for dedupe hashing.

### Two-layer verification
`_compile_urdf_report_impl` executes the generated script via `runpy` (process-wide lock, chdir, sys.path setup — `agent/compiler.py:372-391`), then: (1) requires a top-level `run_tests()` returning a report — its absence is itself a failure (`agent/compiler.py:927-950`); (2) runs a verifier-owned baseline battery on a fresh context with authored allowances replayed, cheap structural gates first with early return so expensive geometry QC never runs on an invalid model (`agent/compiler.py:1174-1216`); (3) merges reports with per-field dedup after dropping baseline results whose check names the author already ran (`agent/compiler.py:985-1120`, name set at `agent/compiler.py:51-63`). Failure raises a ValueError listing at most 10 failures with the full report attached (`agent/compiler.py:1218-1232`).

### Failure exceptions carry structured payloads
On any failure the compiler still best-effort exports the artifact and decorates the wrapper exception with `compiled_urdf_xml`, `warnings`, `test_report`, and a pre-built `compile_signal_bundle` (`agent/compiler.py:161-176`, `agent/compiler.py:264-303`) — the harness checkpoints the last renderable artifact even from failed compiles, and the renderer needs no compiler imports. An `ignore_geom_qc` escape hatch downgrades QC-marker failures to warnings when an artifact exists, for draft/visual-only flows (`agent/compiler.py:35-49`, `agent/compiler.py:188-206`).

### Subprocess isolation with full signal serialization
`compile_urdf_report_maybe_timeout` runs the compile in a killable daemon child (default 300 s; env-tunable; 0 = in-process) (`agent/compiler.py:618-733`). The worker sends a dict: success = `{ok, artifact, warnings, bundle.to_dict()}`; failure = `{ok: False, error, error_type, traceback}` plus attached attrs (`agent/compiler.py:571-615`). The parent rehydrates a RuntimeError with `remote_error_type`/`remote_traceback`/artifact/bundle attributes (`agent/compiler.py:711-732`) — exactly what the feedback module reads, making cross-process failures indistinguishable from in-process ones. Timeout messages name the tunable env vars (`agent/compiler.py:671-678`).

### Justification (allowance) escape hatch
Authored tests can declare `allow_overlap(a, b, reason=...)` / `allow_isolated_part(name, reason=...)`; allowances are replayed onto the verifier-owned baseline context before its checks run (`agent/compiler.py:1149-1171`), so a justified finding does not fail global QC either. In feedback, allowances render as note-severity signals (`agent/feedback.py:818-835`, `agent/feedback.py:950-999`), and the playbook demands exact-pair scoping, a concrete reason, and a paired positive proof check (`agent/feedback.py:1255-1267`).

### Harness feedback loop
`CompileFeedbackLoop` increments `_current_edit_revision` on every mutating tool call (`agent/harness_compile.py:91-94`). `execute_compile_model` short-circuits when the last successful compile's revision equals the current one, re-rendering the cached bundle prefixed "Fresh compile already exists...; treat that compile result as authoritative" (`agent/harness_compile.py:85-89`, `agent/harness_compile.py:146-151`, `agent/harness_compile.py:181-218`). Real compiles checkpoint artifacts on success and from failure exceptions carrying one (`agent/harness_compile.py:162-179`). Repeat detection sha1-hashes the canonical JSON of the whole bundle (`agent/harness_compile.py:123-127`); streaks increment on failure-bearing bundles and reset on clean ones (`agent/harness_compile.py:129-144`). `append_compile_required_reminder` injects a `<compile_required>` user message so the agent cannot conclude on unverified code (`agent/harness_compile.py:96-117`).

### Legacy heuristics off the default path
Geometry scale-anomaly warnings, cwd-relative-path scans, and mesh connectivity checks still exist (`agent/compiler.py:394-423`, `agent/compiler.py:450-568`, `agent/compiler.py:734-767`) but are no longer called during full validation — a test asserts the two warning emitters must NOT run (`tests/agent/test_compiler.py:329-335`; the mesh-connectivity check is uncalled but not covered by that assertion). Their text classifiers remain in `feedback.py` for back-compat with stored records. Lesson: when migrating checks between layers, keep old classifiers, delete or clearly mark dead emitters.

## Comparative: Claude Code's advisory verifier

Articraft's verifier is a **gate**: no fresh compile, no success. Claude Code
has no gate — and it would be wrong to conclude it has no verifier. It has a
substantial one, running continuously, that never blocks. That third position
is the one most real agents actually occupy, and it has its own design rules.

**Tier 2 exists, and it is the common case.** Between "a compiler decides" and
"a human decides" sits: real checkers exist (type checker, linter, tests,
schema validator) but none of them answers "is the task done". The correct
response is not to skip verification — it is to run the checkers and feed their
output back as typed signals the model is expected to act on, while the
authority to refuse a finish attempt lives elsewhere.

**The defining mechanism is a baseline diff scoped by attribution.** Running a
type checker over a real repository prints hundreds of pre-existing problems.
Report them all and the model either fixes unrelated code or learns the channel
is noise; both are worse than silence. Instead, the mutating tools snapshot a
file's diagnostics *before* touching it —
`diagnosticTracker.beforeFileEdited(path)` in
`tools/FileEditTool/FileEditTool.ts:425` and
`tools/FileWriteTool/FileWriteTool.ts:247` — and afterwards only findings absent
from that baseline are surfaced (`services/diagnosticTracking.ts:266-269`).
Three details make it work:

- **Files with no baseline are ignored entirely**
  (`services/diagnosticTracking.ts:210-212`). A problem in a file this agent
  never touched is not attributable to it and not actionable by it.
- **The baseline advances after each report**
  (`services/diagnosticTracking.ts:278-279`), so a finding is delivered exactly
  once. Repeating a known problem trains the model to skim.
- **An empty baseline is recorded explicitly**
  (`services/diagnosticTracking.ts:174-176`). "Clean before" and "never looked"
  must be distinguishable, or the first error in a clean file cannot be
  attributed.

The one thing to add when porting: diff on an identity that **excludes the line
number**. An edit above a pre-existing error shifts its line, and a
line-sensitive key resurfaces it as new — the most common false positive in
this design.

**The severity vocabulary is the same one Articraft uses.** LSP severities map
to `Error | Warning | Info | Hint` (`services/lsp/passiveFeedback.ts:18-35`),
which is Articraft's `failure / warning / note` with one more rung. Two
independently designed agents converging on a small ordered severity enum,
shared across producer, renderer and persistence, is about as strong a signal
as this library gets.

**Feedback is suppressed when the agent cannot act on it.** Diagnostics are not
injected unless the agent has a shell tool — "diagnostics are only useful if
the agent has the Bash tool to act on them"
(`utils/attachments.ts:2857-2862`). Context spent on a report the agent cannot
respond to is worse than wasted: it teaches the model that this channel does
not matter.

**Where the two agents genuinely disagree: one issue, or all of them.**
Articraft selects a single primary issue by a priority ladder, on the grounds
that a model handed N co-equal failures patches the easiest rather than the
causal one. Claude Code reports every new finding at once and keeps the list
short a different way — by attribution-scoping and reporting once, so the list
is naturally small. Neither is universally right: the ladder wins when your
failures are usually one cause with many symptoms (a compile error cascading
into ten QC violations); reporting all wins when they are usually independent
(three unrelated type errors in three files). Choose by which shape your
domain actually produces, and note that only the ladder needs you to *rank*
failure kinds — which is real design work.

**Verification is also delegated outward.** `PostToolUse` hooks run project
linters and tests (reference 12); a `Stop` hook can block a finish attempt and
demand another turn (`query.ts:1267-1306`); a bundled `verify` skill instructs
the agent to actually run the app rather than reason about whether it works
(`skills/bundled/verify.ts:17-29`). The verifier is not one module — it is a
protocol that several subsystems participate in.

**What this means for the library's central rule.** Reference 03's invariant
stands, restated with the middle tier made explicit:

> Build the strongest verifier the domain permits, and be explicit about
> whether it **gates** or **advises**. A gating verifier owns the exit. An
> advisory one owns the feedback channel and must be attribution-scoped and
> report-once, or the model stops reading it. Either way, exactly one authority
> refuses a finished attempt — the verifier if it can, the human if it cannot.
> Zero authorities is the failure mode; an advisory verifier plus a human is
> not zero.

`templates/agentkit/verifier.py` implements the tier-2 pattern.

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Classify free-text check output into a closed typed vocabulary (spec table + needle classifiers) instead of making every check emit structured objects | Feedback layer evolves severity/grouping/advice without touching check emitters; unknown text degrades to a generic fallback, never dropped | String coupling: rewording a check silently demotes it to the fallback spec; needle table must track check wording |
| Exactly ONE "Primary issue" via a hardcoded causal priority ladder | LLMs given N co-equal failures patch the easiest; the ladder encodes root-cause-first (a script that does not run makes all other failures meaningless) | Ordering is domain knowledge frozen in code; new failure kinds default to priority 90 + generic advice until the ladder, summary map, and playbook are all updated |
| Three severity tiers where only `failure` blocks; warnings framed as "design evidence", notes hidden on clean output | Heuristic checks have false positives; blocking on them traps the agent in repair loops on good candidates | The agent can legally ship with real defects that only surfaced as warnings; prose nudge is the only pressure |
| Always run a verifier-owned baseline battery regardless of authored tests, with name-based duplicate filtering and allowance carryover | The agent cannot be trusted to include the safety net; filtering prevents double-reporting; carryover prevents the net overriding legitimate justifications | Two layers double QC cost; exact-name filtering means a parametrized authored variant runs twice |
| Failures travel as exceptions decorated with structured attributes, mirrored field-for-field in the subprocess payload | One error channel serves all callers while delivering everything needed to render feedback and checkpoint partial artifacts | getattr duck typing — attribute typos fail silently; wrapper type forces special-cased location extraction |
| Repeat detection = sha1 of the whole canonical-JSON bundle; streak >= 3 escalates advice toward diagnosis over patching | Exact matching is cheap with zero false positives for "you changed nothing that mattered"; escalation targets the observed LLM failure mode of endless small tweaks | Any cosmetic text change breaks the match — `repeated` has false negatives for same-root-cause failures |
| Cache verify results keyed to an edit-revision counter; unverified code triggers an injected reminder | Verification is expensive (subprocess, up to 300 s); redundant runs become free; the reminder makes the verifier mandatory, not advisory | Freshness inferred from tool names, not file hashes — out-of-band edits serve a stale "authoritative" result |
| Run the compile in a killable child process with hard timeout, serializing the bundle over a Pipe | Generated code hangs, and in-process execution mutates globals (cwd, sys.path); isolation doubles as hygiene | Spawn overhead per verify; exception type flattened to a wrapper + string, traceback becomes text |
| Explicit justification mechanism (allowance with scope + reason) converting blocking findings to notes, with playbook demanding proof checks | Real domains have legitimate rule exceptions; no escape forces lies, a free-form escape invites abuse | Honor-system: the agent can self-authorize past QC; guards are prompt discipline plus the note audit trail |
| Sanitize paths in tracebacks and cap details at 40 lines | Absolute paths waste tokens, leak environment, destabilize dedupe signatures across machines | Occasionally hides which of two similarly-named files a frame came from |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| Verify subprocess timeout | 300 s (env-overridable; 0 = in-process) — `agent/compiler.py:632` | Hard kill for generated-code hangs; message names the tunables |
| Exception detail cap | 40 lines + "... (N more lines)" — `agent/feedback.py:421,455-458` | Bounds token cost of one exception in feedback |
| Failure list cap in raised error | 10 shown, then "... (N more)" — `agent/compiler.py:1224-1229` | Keeps the wrapping error message bounded |
| Streak escalation threshold | >= 3 — `agent/feedback.py:1200-1201,1379` | Two identical failures could be bad luck; three is a loop — switch to diagnostic advice |
| Priority ladder | runtime=0; structural=1; owned QC=2; stale contract=3-4; authored QC=5-6; generic=7; default=90 — `agent/feedback.py:1022-1040` | Deterministic root-cause-first ordering; head = Primary issue |
| Baseline battery check names | 6 fixed checks, cheap structural gates first — `agent/compiler.py:51-63` | The safety net; also the duplicate-filter set |
| Worker join timeout | 2.0 s (after recv and after terminate) — `agent/compiler.py:668,680,684` | Bounded subprocess cleanup |
| Dedupe / repeat hash | sha1 of newline-joined signal fields (per signal); sha1 of canonical bundle JSON (per attempt) — `agent/feedback.py:558-582`, `agent/harness_compile.py:123-127` | Within-bundle dedup; exact cross-attempt repeat detection |
| Distance display threshold | >= 0.1 → "%.3g m", else mm — `agent/feedback.py:614-621` | Human/LLM-readable magnitudes in contract-failure summaries |
| Low-risk heuristic downgrade | `max_risk=low` → note instead of warning — `agent/feedback.py:801-815` | Low-confidence findings should not demand action |
| QC kill switches | `URDF_DISABLE_*` env vars per expensive/heuristic layer — `agent/compiler.py:457,673-674,740` | Every costly check gets an operational escape hatch |

## Reusable pattern

```python
"""Generic evaluator/verifier: execute candidate, two-layer checks, typed
signal bundle, one primary issue, playbook advice, harness feedback loop."""
import hashlib, json
from dataclasses import dataclass, field

# 1) TYPED SIGNAL VOCABULARY -------------------------------------------------
@dataclass(frozen=True)
class Signal:
    severity: str          # "failure" | "warning" | "note"; only failures block
    kind: str              # stable slug, keys the playbook and priority ladder
    code: str              # machine code, e.g. "QC_OVERLAP"
    summary: str
    details: str = ""
    blocking: bool = False
    source: str = "checker"    # "checker" | "authored_tests" | "harness"

    @property
    def dedupe_key(self):
        raw = "\n".join([self.severity, self.kind, self.code,
                         self.summary, self.details, self.source])
        return hashlib.sha1(raw.encode()).hexdigest()

# Closed vocabulary + ordered needle classifiers + fallback (never drop text).
CLASSIFIERS = [
    # (matcher(text) -> bool, builder(headline, details) -> Signal)
]
def classify(text, severity="warning"):
    headline, _, details = text.partition("\n")
    for match, build in CLASSIFIERS:
        if match(text.lower()):
            return build(headline, details)
    return Signal(severity, "generic_" + severity, "GENERIC", headline, details)

# 2) TWO-LAYER VERIFIER (run inside a killable subprocess with hard timeout;
#    worker sends dicts over a Pipe, parent rehydrates identical exceptions) --
BASELINE_CHECKS = []       # verifier-owned safety net, cheap structural first

class VerifyError(RuntimeError):
    def __init__(self, msg, artifact=None, warnings=(), report=None, bundle=None):
        super().__init__(msg)
        self.partial_artifact, self.warnings = artifact, tuple(warnings)
        self.report, self.bundle = report, bundle   # renderer reads attributes

def verify(candidate):
    authored = run_authored_checks(candidate)       # its ABSENCE is a failure
    ctx = fresh_check_context(candidate)
    replay_allowances(ctx, authored)                # justifications carry over
    run_structural_gates(ctx)                       # early-return if invalid
    if not ctx.failed:
        run_expensive_qc(ctx)
    merged = merge_dedup(authored, drop_already_run(ctx.report(), authored))
    if merged.failures:
        bundle = build_bundle("failure", raw_warnings=(), report=merged, exc=None)
        raise VerifyError(head(merged.failures, 10),
                          artifact=best_effort_export(candidate),
                          warnings=merged.warnings, report=merged, bundle=bundle)
    return merged

# 3) BUNDLE + PRIMARY ISSUE --------------------------------------------------
PRIORITY = {"runtime_error": 0, "structural_policy": 1, "owned_global_qc": 2,
            "stale_contract": 3, "authored_qc": 5, "generic_failure": 7}
ONE_LINER = {}             # kind -> canonical "Primary issue: ..." sentence
RULES_BY_KIND = {}         # kind -> 2-7 imperative bullets, root-cause-first
ESCALATION_BY_KIND = {}    # kind -> loop-breaking advice at streak >= 3

def build_bundle(status, raw_warnings, report, exc):
    sig = {}                                        # keyed by dedupe_key
    for w in raw_warnings:
        if w not in report.warnings:                # skip verbatim duplicates
            s = classify(w); sig[s.dedupe_key] = s
    for w in report.warnings:
        s = classify(w); sig[s.dedupe_key] = s
    for a in report.allowances:                     # justifications -> notes
        s = allowance_note(a); sig[s.dedupe_key] = s
    fails = [classify_failure(f) for f in report.failures]
    if fails:
        for s in fails: sig[s.dedupe_key] = s
    elif exc is not None:                           # exception ONLY when no
        s = runtime_signal(exc)                     # structured failures exist
        sig[s.dedupe_key] = s
    return status, tuple(sig.values())

def render(status, signals, repeated=False, streak=0):
    fails = sorted((s for s in signals if s.severity == "failure"),
                   key=lambda s: (PRIORITY.get(s.kind, 90), s.kind, s.summary))
    warns = [s for s in signals if s.severity == "warning"]
    notes = [s for s in signals if s.severity == "note"]
    out = [f"<signals>", f"<summary>status={status} failures={len(fails)} "
           f"warnings={len(warns)} notes={len(notes)}"]
    if fails:
        out.append("Primary issue: " + ONE_LINER.get(fails[0].kind, fails[0].summary))
        if repeated: out.append("This failure matches the previous attempt.")
        if streak >= 3: out.append(f"This is failure {streak} in a row.")
    out.append("</summary>")
    def section(tag, label, items):
        if not items: return
        out.append(f"<{tag}>{label}")
        for s in items:
            out.append(f"- {s.severity.upper()} [{s.kind}] {s.summary}")
            out.extend("  " + ln for ln in s.details.splitlines())
        out.append(f"</{tag}>")
    section("failures", "Failures (blocking):", fails)
    section("warnings", "Warnings (non-blocking):", warns)
    if fails or warns:                              # notes hidden on clean runs
        section("notes", "Notes (informational):", notes)
    if fails or warns:
        out.append("<response_rules>Suggested next steps:")
        if fails:
            out.extend(RULES_BY_KIND.get(fails[0].kind, ["- Fix the primary issue first."]))
            if streak >= 3:
                out.extend(ESCALATION_BY_KIND.get(fails[0].kind,
                           ["- Stop tweaking; run a diagnostic before the next edit."]))
        if warns:
            out.append("- Warnings are design evidence; address or justify them.")
        out.append("</response_rules>")
    out.append("</signals>")
    return "\n".join(out)

# 4) HARNESS FEEDBACK LOOP (wraps the verify tool) ---------------------------
class FeedbackLoop:
    def __init__(self):
        self.edit_rev = 0; self.ok_rev = None; self.cached = None
        self.last_sig = None; self.streak = 0

    def on_mutating_tool(self):                     # bump on write/edit tools
        self.edit_rev += 1

    def run_verify(self, candidate):
        if self.cached and self.ok_rev == self.edit_rev:
            return ("Fresh verify result already exists for this code; "
                    "treat it as authoritative.\n" + render(*self.cached))
        try:
            report = verify_in_subprocess(candidate, timeout_s=300)
            status, signals = build_bundle("success", report.raw_warnings, report, None)
        except VerifyError as e:
            checkpoint(e.partial_artifact)          # keep last good artifact
            status, signals = e.bundle or build_bundle("failure", e.warnings, e.report, e)
        sig = hashlib.sha1(json.dumps(
            [s.__dict__ for s in signals], sort_keys=True).encode()).hexdigest()
        failed = any(s.severity == "failure" for s in signals)
        repeated = failed and sig == self.last_sig
        if failed: self.streak += 1; self.last_sig = sig
        else:
            self.streak = 0; self.last_sig = None
            self.ok_rev = self.edit_rev; self.cached = (status, signals)
        return render(status, signals, repeated, self.streak)

    def before_conclude(self):                      # verifier is mandatory
        if self.ok_rev != self.edit_rev:
            inject_user_message("<verify_required>Code changed since the last "
                                "successful verify. Run the verifier before "
                                "concluding.</verify_required>")
```

## Pitfalls

- Needle/prefix classification couples the feedback layer to exact check wording; rewording a check silently demotes its signal to the generic fallback with generic advice. Keep one integration test per needle (Articraft: `tests/agent/test_feedback_compile_hints.py`).
- Whole-bundle hashing makes `repeated` false-negative on cosmetic diffs (a changed magnitude in one detail line reads as a new failure). If you want family-level repetition, hash only `(kind, code, check_name)` of the failures.
- Enforce the exception-vs-report precedence: synthesize a runtime-failure signal only when zero structured failures exist (`agent/feedback.py:1128-1134`), or every failed test run also shows a redundant "tests failed" exception signal.
- If your verifier wraps failures in a common exception type, location extraction must skip wrapper frames — but then a genuine user-code exception of that same type loses its location unless a remote traceback rides along. Prefer attaching the original traceback text explicitly over type-based filtering.
- Suppressing notes on clean output keeps feedback terse but hides declared allowances from the transcript — the agent cannot re-audit its exception state. Decide deliberately.
- Exact-name duplicate filtering between the authored and owned check layers misses parametrized variants (different tolerance args), which then run twice with potentially conflicting results.
- In-process script execution mutates process globals (cwd, `sys.path`); Articraft guards it with a lock and a finally-restore, and it is safe only because real runs use a subprocess. Copy both guards if you copy the in-process path.
- Timeouts skip the failure-artifact checkpoint (`agent/harness_compile.py:202-209`), so a hang after a near-complete build saves nothing — note the asymmetry, decide if you need a pre-timeout snapshot.
- Freshness tracked by tool names, not content hashes, breaks on out-of-band file edits — the loop serves a stale result labeled "authoritative". Hash file contents if external editors can touch the workspace.
- The allowance mechanism is honor-system. Budget for prompt-side discipline (exact scope + reason + proof check) and an auditable note trail; do not pretend it is enforcement.
- When migrating checks out of a layer, delete or clearly mark the dead emitters and add a test asserting they no longer run (Articraft does this for two of its three legacy emitters: `tests/agent/test_compiler.py:329-335`), but keep their text classifiers for back-compat with stored records.

## Checklist

**First, decide the tier** — a checklist for the wrong one is worse than none:

- [ ] Stated explicitly whether your verifier **gates** the exit or **advises**
- [ ] Exactly one authority can refuse a finished attempt (verifier, or human)

*If advisory (tier 2 — the common case):*

- [ ] Findings are diffed against a baseline captured **before** the change
- [ ] The diff identity excludes the line number but preserves multiplicity,
      so a shifted error is not "new" and a second occurrence still is
- [ ] Paths are normalised on both write and read, or a relative/absolute
      mismatch silently discards every finding
- [ ] A checker that could not run is distinguishable from a clean result, and
      does **not** advance the baseline
- [ ] Findings for files the agent never touched are dropped, not reported
- [ ] Each finding is reported exactly once
- [ ] Severity is normalised on construction, so a checker's own vocabulary
      cannot crash the diff or silently fail to block
- [ ] Reports are capped, ordered by severity, and say what they omitted
- [ ] Nothing is injected when the agent has no tool that could act on it
- [ ] The tracker is reset per user turn, and renames move their baseline

*If gating (tier 1):*


- [ ] Define a frozen signal type: severity (only `failure` blocks), stable kind/code slugs, source, details, sha1 dedupe key.
- [ ] Pre-declare a spec table for every known finding class; add a generic fallback spec so no text is ever dropped.
- [ ] Write text→signal classifiers (needles, prefixes, regex magnitude extraction) with one integration test per rule.
- [ ] Require agent-authored checks to exist (absence = failure) AND always run a verifier-owned baseline battery.
- [ ] Order the baseline cheap-structural-first with early return; filter duplicates by check name; replay authored allowances onto the owned context.
- [ ] Run verification in a killable subprocess with a hard timeout; serialize the full bundle plus partial artifact across the pipe; rehydrate exceptions with identical attributes.
- [ ] Decorate failure exceptions with the partial artifact, warnings, report, and pre-built bundle; checkpoint artifacts even from failures.
- [ ] Build the bundle keyed by dedupe key; synthesize a runtime-error signal only when no structured failures exist.
- [ ] Hardcode a causal priority ladder and pick exactly one Primary issue; map each kind to a canonical one-liner.
- [ ] Render tagged sections (summary/failures/warnings/notes/response_rules); suppress notes on clean output.
- [ ] Write a 2-7 bullet playbook per failure family; append "warnings are evidence" when warnings coexist; escalate to diagnose-over-patch advice at streak >= 3.
- [ ] Cap exception details (~40 lines), sanitize paths, strip wrapper frames, append canned hints for known-confusing errors.
- [ ] Wrap the verify tool in a harness loop: edit-revision counter, cached-fresh short-circuit with an "authoritative" preamble, sha1 repeat detection, streak counter, and an injected "verify required before concluding" reminder.
- [ ] Provide a justification escape hatch (scope + reason) that downgrades findings to notes, with playbook rules demanding exact scoping and a paired proof check.
- [ ] Add env-var kill switches for every expensive or heuristic check layer.
