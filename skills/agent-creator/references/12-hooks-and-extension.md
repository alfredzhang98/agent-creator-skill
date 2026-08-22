# 12. Hooks & Extension Points

**Maps to:** Guardrails · Executor · Orchestration · **Distilled from:** Claude Code 2.1.88 — `src/utils/hooks.ts`, `src/services/tools/toolHooks.ts`, `src/entrypoints/sdk/coreSchemas.ts`, `src/query/stopHooks.ts`

## Why this module exists

Every agent that reaches more than one team acquires requirements you will not
implement: run the linter after each edit, refuse commits that touch
`infra/`, log every shell command to the audit pipeline, inject the current
sprint number into each turn. Without an extension seam these arrive as forks,
as prompt bloat, or as wrappers that shell out to your CLI and parse its
stdout.

A **hook** is a named lifecycle point where somebody else's code runs with a
typed payload and a typed reply. It is the difference between "your agent" and
"a platform". It is also the most dangerous thing in the system — hooks execute
with the agent's authority, are configured in files that travel with
repositories, and can silently change what the model sees. So the contract has
to be unusually explicit about three things: **when** they run, **how** they
say no, and **what happens when they are wrong**.

## How Claude Code implements it

### A closed event vocabulary

27 named events (`entrypoints/sdk/coreSchemas.ts:355-383`). The 24 grouped
below cover the tool
lifecycle (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`,
`PermissionRequest`, `PermissionDenied`), the session (`SessionStart`,
`SessionEnd`, `Setup`, `UserPromptSubmit`), the turn (`Stop`, `StopFailure`),
context management (`PreCompact`, `PostCompact`), delegation (`SubagentStart`,
`SubagentStop`, `TaskCreated`, `TaskCompleted`, `TeammateIdle`), and the
environment (`CwdChanged`, `FileChanged`, `WorktreeCreate`, `WorktreeRemove`,
`ConfigChange`, `InstructionsLoaded`). The remaining three are `Notification`
and the elicitation pair (`Elicitation`, `ElicitationResult`). The set is closed and validated by an
enum schema — an unrecognised event name is a configuration error the user is
told about, not a hook that silently never fires.

The shape of the list is itself the lesson: *failure* events are separate from
success events (`PostToolUseFailure`, `StopFailure`), and *paired* lifecycle
events exist for anything with a beginning and an end. A hook author who wants
"when a subagent finishes badly" should not have to reconstruct it.

### Two reply protocols, because two kinds of hook exist

An exit code is enough for a script that only needs to say pass/fail: **0**
success, **2** blocking error with stderr fed back to the model, anything else
a non-blocking error that is logged and ignored (`utils/hooks.ts:2647,
3328`). A hook that wants to do more emits JSON on stdout: change the
permission decision, rewrite the tool input, inject context, set a
user-visible message, or halt the turn (`utils/hooks.ts:489-570`).

Exit code and JSON are not alternatives — they compose, and the exit code is
authoritative about blocking. A hook that exits 2 has blocked whatever its
stdout claimed.

### The error message carries the schema

When a hook's JSON fails validation, the error handed back names the entire
expected shape inline — every field, every enum, every per-event variant
(`utils/hooks.ts:415-446`). A hook author debugging at 2am does not have to
find the docs. This is the same principle as an actionable tool error, applied
to the humans extending your agent rather than the model using it.

### Trust is checked before execution — with one mode-level exception

`shouldSkipHookDueToTrust` (`utils/hooks.ts:286-296`) blocks hooks in an
untrusted workspace, and the comment records why the rule has no *per-event*
exceptions: two historical vulnerabilities where `SessionEnd` and
`SubagentStop` hooks executed before the trust dialog resolved
(`utils/hooks.ts:280-283`). The lesson generalises — **a trust gate with
per-event exceptions is a trust gate with holes**, because the exempted paths
are always the ones nobody was thinking about.

But read the function before copying the slogan. It opens:

```ts
const isInteractive = !getIsNonInteractiveSession()
if (!isInteractive) { return false }   // SDK: trust is implicit
```

There **is** an exception, and it is mode-level rather than per-event: in
non-interactive/SDK sessions every hook runs, because there is no dialog to
show. That is exactly the deployment an agent-builder is in. So the honest
rule is: *no per-event exemptions, and one deliberate mode-level one whose
premise — "someone approved this configuration out of band" — you must
actually satisfy.* If your headless deployment loads hooks from a cloned
repository, that premise is false and you need your own gate.

### Blocking is feedback, not failure

A `PreToolUse` hook that blocks turns into a tool_result the model reads and
can respond to. A `Stop` hook that blocks does not end the run — it feeds its
message back and the loop continues (`query.ts:1282-1306`), which is how "you
forgot to run the tests" becomes another turn instead of an error. The model is
told to treat hook feedback as coming from the user
(`constants/prompts.ts:127-129`).

The corresponding hazard is spelled out at the call site: stop hooks are
**skipped entirely** when the last message is an API error, because
`error → hook blocks → retry → error` is a death spiral that injects more
tokens each cycle (`query.ts:1258-1265, 1168-1175`). Any hook that can force
another turn needs an explicit answer to "what stops this looping?"

### Async hooks and the wake-up path

A hook may return an async response and keep running in the background
(`utils/hooks.ts:184-265`). If it later exits 2, its output is enqueued as a
task-notification that either wakes an idle model or is injected mid-query as
an attachment (`utils/hooks.ts:236-243`). This is what lets a 90-second test
suite be a hook without blocking the turn on it.

### Timeouts are per-purpose, not global

Tool hooks get 10 minutes (`utils/hooks.ts:166`); `SessionEnd` hooks get
**1.5 seconds** by default, env-overridable (`utils/hooks.ts:168-182`),
because they run during shutdown where a hanging hook is indistinguishable
from a hung program. One global hook timeout is always wrong at one end.

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Closed, validated event enum | An unknown event name is a typo the user can be told about, rather than a hook that mysteriously never runs | Every new lifecycle point is an API addition; third parties cannot add events |
| Both exit-code and JSON protocols | A one-line shell script and a policy engine are both legitimate hooks; forcing JSON on the former kills adoption | Two paths to keep consistent; the precedence rule (exit code wins on blocking) must be documented or authors guess |
| Validation errors embed the full expected schema | The audience is a human debugging their own script; a schema in the error is the difference between a fix and a filed issue | Verbose errors; the schema hint must be regenerated when the shape changes |
| Trust gate applies to *all* hooks, unconditionally | Two real vulnerabilities came from per-event exceptions; the exceptions are always the unconsidered paths | Legitimate teardown hooks do not run in an untrusted workspace |
| Blocking feeds back rather than terminating | "You forgot the tests" should produce another turn, not a failed run | Creates loop risk; needs an explicit circuit breaker |
| Stop hooks skipped after API errors | `error → block → retry → error` injects more tokens each cycle | A hook that wanted to observe failures needs the separate `StopFailure` event |
| Most-restrictive-wins aggregation | Otherwise hook *ordering* silently becomes a security parameter | A single overly cautious hook can block work no one intended it to |
| Async hooks with a notification wake-up | A slow check should not cost every turn its latency | Its verdict arrives after the action it was checking |
| Per-purpose timeouts | Shutdown hooks and test-suite hooks differ by three orders of magnitude | More constants to tune |
| Hooks may rewrite tool input | The cleanest way to enforce house style (add `--no-verify` guards, normalise paths) without prompt bloat | The model's transcript no longer shows what actually ran unless you record both |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| Hook events | 27 (`entrypoints/sdk/coreSchemas.ts:355-383`) | Closed set; failure and paired-lifecycle events are separate entries |
| Blocking exit code | `2` (`utils/hooks.ts:236, 2647`) | Distinct from 0 (ok) and 1 (script error), which must NOT block |
| Tool hook timeout | 600,000 ms (`utils/hooks.ts:166`) | Long enough for a real test suite |
| SessionEnd hook timeout | 1,500 ms, env-overridable (`utils/hooks.ts:175-182`) | Runs during shutdown; also the overall parallel-hook cap |
| Inline timing display | 500 ms (`services/tools/toolExecution.ts:134`) | Show a hook-timing summary once hooks are slow enough to notice |
| Slow-phase warning | 2,000 ms (`services/tools/toolExecution.ts:137`) | "the collapsed view feels stuck past this" — the point at which a hook needs a log line |
| Trust requirement | all events, no exceptions (`utils/hooks.ts:267-296`) | Two CVE-shaped bugs came from per-event exceptions |

## Reusable pattern

See `templates/agentkit/hooks.py` for the runnable version — matcher
filtering, both protocols, strictness-ordered aggregation, and a refusing
default executor. The shape:

```python
EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", ...)
EXIT_OK, EXIT_BLOCKING = 0, 2

def parse(run, spec):
    out = Outcome()
    if run.stdout.strip().startswith("{"):
        try:
            apply_json(out, json.loads(run.stdout))
        except json.JSONDecodeError as e:
            # The audience is a human. Name the shape you wanted.
            out.system_message = f"Invalid JSON ({e}).\nExpected:\n{SCHEMA_HINT}"
    if run.exit_code == EXIT_BLOCKING:          # authoritative about blocking
        out.blocking_error = run.stderr or run.stdout or "Blocked by hook"
        out.permission = out.permission or "deny"
    elif run.exit_code != EXIT_OK:
        out.system_message = f"Hook failed (exit {run.exit_code})"   # operator only
    return out

STRICTNESS = {"allow": 0, "ask": 1, "deny": 2}   # most restrictive wins,
                                                 # so ordering is not a policy

def run_hooks(specs, event, payload, executor):
    if event not in EVENTS:
        raise ValueError(f"unknown hook event {event!r}")   # loud, not silent
    outcomes = []
    for spec in matching(specs, event, payload.get("tool_name")):
        try:
            outcomes.append(parse(executor.run(spec, payload), spec))
        except Exception as exc:
            # A broken hook contributes nothing. It must not take the agent
            # down, and it must not become an implicit approval either.
            outcomes.append(Outcome(ok=False, system_message=f"Hook error: {exc}"))
    return aggregate(outcomes)
```

Wiring rules that matter as much as the code:

- Run `PreToolUse` **before** the permission check, so a hook can decide; run
  `PostToolUse` after, so it can judge the result.
- A hook that returns `updatedInput` owns the shape from then on — do not
  re-apply your own backfill on top of it.
- Give the executor a trust gate and a hard timeout; both are the deployment's
  call, which is why the default should refuse rather than guess.

## Pitfalls

- **A trust gate with exceptions.** Every "this event is harmless" exemption is
  the one that fires before the dialog. Gate all of them.
- **Letting hook order decide.** Without a strictness order, whichever hook ran
  last wins, and adding an unrelated hook changes a security outcome.
- **Blocking with no circuit breaker.** A `Stop` hook that blocks forever, or
  one that fires on an API error, burns thousands of calls. Skip stop hooks
  after API errors, and cap consecutive blocks.
- **Swallowing hook failures silently.** A non-blocking error must reach the
  *operator*; it must never reach the model as if it were a result, and must
  never count as approval.
- **Terse validation errors.** "Invalid JSON" sends the author to your source.
  Embed the schema.
- **Forgetting the failure events.** Without `PostToolUseFailure` and
  `StopFailure`, every observability hook has to infer failure from absence.
- **Hooks that rewrite input invisibly.** Record both the model's input and the
  rewritten one, or your transcript is a work of fiction.
- **One global timeout.** Shutdown hooks need ~1s; test hooks need ~10min.

## Checklist

- [ ] Event names are a closed, validated set; unknown names raise loudly
- [ ] Both an exit-code and a JSON protocol, with the precedence documented
- [ ] Validation failures embed the full expected schema
- [ ] A trust gate applies to every event with no exceptions
- [ ] Blocking produces model-visible feedback, not a terminated run
- [ ] Stop-class hooks are skipped after API errors, and consecutive blocks are capped
- [ ] Aggregation is most-restrictive-wins, so ordering is not a policy
- [ ] A crashing hook is recorded, skipped, and never counted as approval
- [ ] Timeouts are per-purpose, with the shutdown path much tighter
- [ ] Paired lifecycle events and explicit failure events both exist
- [ ] Input rewrites are recorded alongside the original
