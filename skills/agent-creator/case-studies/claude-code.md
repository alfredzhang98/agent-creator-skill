# Case study 02: Claude Code

**Domain:** general-purpose coding agent — open-ended repository work in a terminal, with a human in the loop
**Source:** `@anthropic-ai/claude-code` 2.1.88, npm tarball with an attached source map; all citations are `src/`-relative paths in the sourcemap-extracted tree
**Distilled:** 2026-08 — full pass over the non-UI source (~377k lines across 1,262 files, excluding the terminal UI): turn loop, tool layer, tool-call lifecycle, permissions, hooks, sandbox, context management, cost, skills, and subagent orchestration. Coverage map at the end.

## What this agent does

Claude Code sits opposite [Articraft](articraft.md) on the axis that matters
most for agent design: **what decides that the work is done.** Articraft
generates a CAD script and a compiler says yes or no. Claude Code edits
arbitrary repositories toward goals stated in English — there is no oracle for
"make this code better", and the operator is a human at a terminal who can
interrupt at any turn.

The distinction is narrower than "verifier vs no verifier", and getting it
right is the point of this case study. Claude Code has **plenty** of mechanical
verification — LSP diagnostics, linters and test suites run through hooks, a
`verify` skill. What it does not have is a *gate*: no check can refuse a finish
attempt. Its verification is **advisory** — it produces typed signals the model
is expected to act on, while the authority to say "not done" lives with the
human (and with `Stop` hooks acting on the human's behalf).

So the architecture reallocates. With no gating oracle, the load-bearing
invariants move to *the action boundary*: what the model may attempt, what it
must ask about first, and how much of the capability surface is in context at
any moment. Articraft spends its engineering on the verifier; Claude Code
spends its on a permission system, a sandbox, progressive disclosure — and a
passive verifier that reports without blocking.

## Architecture (tool + skill layers)

```
                     model turn
                         │
                  tool_use block
                         │
 services/tools/toolExecution.ts — checkPermissionsAndCallTool
   1. zod schema parse ............... InputValidationError → tool_result(is_error)
   2. tool.validateInput() ........... <tool_use_error> → tool_result(is_error)
   3. speculative classifier start ... (runs in parallel with 4-6)
   4. strip internal-only fields ..... defense in depth vs model-supplied fields
   5. backfillObservableInput() ...... on a CLONE; API-bound input never mutated
   6. PreToolUse hooks ............... may add context / update input / decide / stop
   7. permission decision ............ hook result > canUseTool > deny/allow rules
   8. tool.call() .................... the only step that touches the world
   9. mapToolResultToToolResultBlockParam()
  10. overflow → utils/toolResultStorage.ts (persist to disk, return preview+path)
                         │
                    tool_result
```

```
 query.ts — the turn loop, as a state machine with named transitions
   per iteration, BEFORE the model call:
     result budget -> snip -> microcompact -> collapse -> autocompact
     (cheapest and most reversible first; each may make the next unnecessary)
   model call (streaming; tools begin executing as tool_use blocks arrive)
   recoverable errors are WITHHELD while a ladder runs:
     context too long  -> drain collapses -> reactive compact -> surface
     output truncated  -> raise cap       -> resume (x3)      -> surface
     model unavailable -> switch model, discard partials, restart request
   no tool calls  -> Stop hooks (may block -> another turn) -> Terminal
   tool calls     -> gauntlet -> results -> attachments -> next turn
   exits: completed | max_turns | blocking_limit | prompt_too_long |
          image_error | model_error | aborted_streaming | aborted_tools |
          hook_stopped | stop_hook_prevented
```

```
 Tool.ts — one ~40-member contract per tool, built through buildTool()
   identity ....... name, aliases, searchHint, inputSchema, outputSchema, prompt
   semantics ...... isReadOnly, isConcurrencySafe, isDestructive, isOpenWorld,
                    isEnabled, interruptBehavior, isSearchOrReadCommand
   gating ......... validateInput, checkPermissions, preparePermissionMatcher,
                    shouldDefer, alwaysLoad, maxResultSizeChars
   observability .. toAutoClassifierInput, backfillObservableInput,
                    getActivityDescription, getToolUseSummary, extractSearchText
   rendering ...... renderToolUseMessage / Result / Progress / Rejected / Error /
                    GroupedToolUse, isResultTruncated, renderToolUseTag

 skills/loadSkillsDir.ts — capability, not action
   5 roots (policy · user · project · --add-dir · legacy) + bundled + MCP
   → parse SKILL.md frontmatter (total defaults)
   → dedup by realpath
   → split unconditional / conditional(paths:)
   → index rendered into ~1% of the context window
   → body loaded ONLY when SkillTool is invoked
```

## The numbers that matter (production-tuned constants)

| Guardrail | Value | Source |
|---|---|---|
| Tool result → disk threshold | `min(tool.maxResultSizeChars, 50_000)` chars; preview 2,000 bytes + file path | `constants/toolLimits.ts:13`, `utils/toolResultStorage.ts:109` |
| Per-message aggregate cap | 200,000 chars across one turn's parallel tool_results | `constants/toolLimits.ts:49` |
| Max tool result | 100,000 tokens (~400 KB) | `constants/toolLimits.ts:22` |
| Per-tool caps | Bash 30,000 · Grep 20,000 · Read `Infinity` (never persist) · everything else 100,000 | `tools/BashTool/BashTool.tsx:424`, `tools/GrepTool/GrepTool.ts:164`, `tools/FileReadTool/FileReadTool.ts:342` |
| Bash timeout | default 120,000 ms, max 600,000 ms (both env-tunable) | `utils/timeouts.ts:2-3` |
| Read defaults | 2,000 lines, 25,000 output tokens | `tools/FileReadTool/prompt.ts:10`, `tools/FileReadTool/limits.ts:18` |
| Grep default head limit | 250 matches | `tools/GrepTool/GrepTool.ts:108` |
| Skill index budget | 1% of context window × 4 chars/token, fallback 8,000 chars; 250 chars/entry; names-only below 20 | `tools/SkillTool/prompt.ts:21-29, 68` |
| Tool-search auto threshold | deferrable schemas > 10% of context window | `utils/toolSearch.ts:45-49` |
| Deferred tools | 24 of 42 tool directories declare `shouldDefer: true` (26 declaration sites); MCP tools always deferred | `grep -rn '^\s*shouldDefer: true,' src/tools/` |
| Hook events | 27 named events | `entrypoints/sdk/coreSchemas.ts:355-383` |
| Sandbox default | `autoAllowBashIfSandboxed` defaults **true** — sandboxed commands skip the prompt | `utils/sandbox/sandbox-adapter.ts:471` |
| Bash tool composition | ~8,500 of ~12,400 lines are permission/security/validation, not execution | `src/tools/BashTool/` |

*Loop, context and cost:*

| Guardrail | Value | Source |
|---|---|---|
| Loop exit reasons | 10 named `Terminal` reasons; 7 named `Continue` transitions | `query.ts:646-1725` |
| Output-truncation recovery | escalate the cap once, then 3 resume attempts | `query.ts:164, 1188-1252` |
| Autocompact threshold | context window − 13,000 tokens; warn/error bands 20,000 below; manual reserve 3,000 | `services/compact/autoCompact.ts:62-70` |
| Autocompact circuit breaker | 3 consecutive failures | `autoCompact.ts:70` |
| Compaction summary cap | 20,000 output tokens | `autoCompact.ts:30` |
| Post-compact restore | ≤5 files, 50,000 tokens, ≤5,000 per file | `services/compact/compact.ts:122-124` |
| Microcompactable tools | Read, shell, Grep, Glob, WebSearch, WebFetch, Edit, Write | `services/compact/microCompact.ts:41-50` |
| Todo reminder cadence | after 10 turns without a write, at most every 10 turns | `utils/attachments.ts:254-257` |
| Memory injection budget | 200 lines / 4 KB per file / **60 KB per session**, then prefetch stops | `utils/attachments.ts:269-289` |
| Hook timeouts | tool hooks 600,000 ms; SessionEnd 1,500 ms | `utils/hooks.ts:166, 175-182` |
| Hook blocking signal | exit code 2 (stderr returned to the model) | `utils/hooks.ts:236, 2647` |
| Bypass-immune permission steps | 1d, 1e, 1f, 1g — evaluated *before* bypass mode at 2a | `utils/permissions/permissions.ts:1225-1281` |
| Unknown model pricing | default price + session flag + "costs may be inaccurate" annotation | `utils/modelCost.ts:166-172`, `cost-tracker.ts:228-233` |
| Subagent context saving | `omitClaudeMd` ≈ 5-15 Gtok/week across 34M+ Explore spawns | `tools/AgentTool/loadAgentsDir.ts:128-132` |

## The design moves worth stealing

1. **Semantics are declared, not inferred** (`Tool.ts:402-437`). Every tool
   answers `isReadOnly`, `isConcurrencySafe`, `isDestructive`, `isOpenWorld`,
   `isEnabled`, and `interruptBehavior` as *functions of the input*, not of
   the tool. `Bash("ls")` is read-only; `Bash("rm -rf")` is not. The harness
   then derives parallelism, permission strictness, interrupt handling, and
   UI collapsing from those answers instead of maintaining a table of tool
   names — which is what makes a plugin-supplied tool a first-class citizen.

2. **Defaults are fail-closed, in exactly one place** (`Tool.ts:757-792`).
   `buildTool()` fills `isConcurrencySafe → false`, `isReadOnly → false`,
   `isDestructive → false`, `toAutoClassifierInput → ''`. A tool author who
   forgets a method gets the *conservative* behaviour — serialised, treated
   as a write — and never `?.() ?? default` scattered across call sites. The
   one non-conservative default (`checkPermissions → allow`) is deliberate:
   it delegates to the general permission system rather than short-circuiting
   it.

3. **A six-stage gauntlet before `call()`, every failure returned in-band**
   (`services/tools/toolExecution.ts:599-1210`). Schema parse, then
   tool-specific `validateInput`, then field stripping, then hooks, then
   permission — and each failure becomes a
   `<tool_use_error>…</tool_use_error>` tool_result with `is_error: true`,
   never a raised exception. This is the same invariant Articraft enforces
   ("tool errors are data"), implemented at a scale where the error taxonomy
   itself matters: the model is told *which* stage rejected it.

4. **Never mutate the API-bound input** (`Tool.ts:474-481`,
   `toolExecution.ts:775-793, 1181-1206`). Derived and legacy fields are
   backfilled onto a *shallow clone* that hooks, permission checks, and the
   transcript observe; `call()` receives the model's original values. The
   stated reason is twofold — prompt-cache preservation, and keeping the
   tool-result string (which embeds the path the model wrote) byte-stable so
   transcript and fixture hashes do not drift. Two consumers, two shapes, one
   source of truth.

5. **Big results overflow to disk, not to truncation**
   (`utils/toolResultStorage.ts`). Past the threshold the result is written
   to `<session>/tool-results/<id>`, and the model receives a 2,000-byte
   preview wrapped in `<persisted-output>` plus the path — so it can `Read`
   the rest with an offset if it actually needs it. Truncation destroys
   information; persistence relocates it. The budget is enforced per
   *message* as well as per tool (200k chars), because ten parallel tools each
   under their own cap can still bury a turn.

6. **`Read` declares `maxResultSizeChars: Infinity`, on purpose**
   (`Tool.ts:457-466`). Persisting a read result would create a
   Read → file → Read loop, and the tool already self-bounds at 2,000 lines /
   25,000 tokens. The general mechanism has one documented exemption, and the
   exemption carries its reasoning inline — a pattern worth copying whenever
   a cross-cutting policy meets a tool that already solves the same problem.

7. **Progressive disclosure, measured** — 24 of 42 tool directories ship as *names only*
   and are fetched on demand through a search tool
   (`tools/ToolSearchTool/prompt.ts:62-108`), gated on deferrable schemas
   exceeding 10% of the context window (`utils/toolSearch.ts:45-49`). Skills
   use the identical ladder one level up. The negative result is recorded in
   the source: rendering each deferred tool's one-line hint in the
   announcement was A/B-tested and showed **no benefit**, so the announcement
   is the bare name (`tools/ToolSearchTool/prompt.ts:110-117`). See
   [reference 11](../references/11-skills-progressive-disclosure.md).

8. **Volatile lists never live in a cached prefix** (`tools/AgentTool/prompt.ts:48-59`).
   The subagent list was interpolated into a tool description; MCP servers
   connecting asynchronously mutated it; each mutation busted the entire
   tool-schema cache — **~10.2% of fleet cache-creation tokens**. It now
   arrives as an attachment message. The same reasoning produces a one-line
   normalisation elsewhere: the per-UID temp directory is rewritten to the
   literal `$TMPDIR` so the shell tool's prompt is byte-identical across
   users and shares a cross-user global cache (`tools/BashTool/prompt.ts:185-190`).

9. **Isolation buys autonomy** (`utils/sandbox/sandbox-adapter.ts:471`,
   `tools/BashTool/bashPermissions.ts:1356`). `autoAllowBashIfSandboxed`
   defaults to **true**: a command that runs inside the OS sandbox skips the
   permission prompt entirely. The sandbox is not a tax on the agent, it is
   what pays for the agent's independence — and the prompt tells the model
   how to recognise a sandbox-caused failure and retry with an explicit
   override *without asking first*, because that path re-enters the
   permission dialog anyway (`tools/BashTool/prompt.ts:228-256`).

10. **The loop is a state machine with named transitions.** Ten `Terminal`
    exits, seven `Continue` reasons, all cross-iteration state in one object
    rewritten wholesale at each continue site (`query.ts:204-217, 646-1725`).
    The source's stated motive is testability; the operational payoff is that
    "why did this stop?" is a value, not an investigation.

11. **Recovery is a ladder whose rungs fire once each.** Context overflow:
    drain collapses → summarise → surface. Output truncation: raise the cap →
    resume ×3 → surface. Each rung guarded by a flag, because resetting one of
    them once produced an infinite compact-retry cycle "burning thousands of
    API calls" (`query.ts:1085-1252, 1292-1297`).

12. **Recoverable errors are withheld until recovery is known to fail**
    (`query.ts:788-825, 166-172`). Yielding early leaks an intermediate error
    to consumers that terminate on it — "the recovery loop keeps running but
    nobody is listening."

13. **Five context mechanisms, ordered cheapest-and-most-reversible first**
    (`query.ts:365-467`): per-message result budget → snip → microcompact →
    context collapse → autocompact. Collapse runs before autocompact
    specifically so that if it gets under the threshold, "we keep granular
    context instead of a single summary."

14. **Cache-breaking is opt-in and must be justified in the API.**
    `DANGEROUS_uncachedSystemPromptSection(name, compute, reason)` requires a
    reason argument (`constants/systemPromptSections.ts:32-38`), and a single
    marker separates cross-organisation-cacheable content from the volatile
    tail (`constants/prompts.ts:105-115`).

15. **Bypass mode cannot override deny, required-interaction, or safety
    checks** (`utils/permissions/permissions.ts:1225-1281`). "Skip the prompts"
    is a statement about convenience, not authority — the difference between a
    mode and a master key.

16. **A trust gate with no exceptions.** All hooks require workspace trust, and
    the source records the two vulnerabilities that produced the rule: a
    `SessionEnd` and a `SubagentStop` hook that executed before the trust
    dialog resolved (`utils/hooks.ts:267-296`).

17. **Subagents are declarative, and their allowlists replace rather than
    extend** (`tools/AgentTool/loadAgentsDir.ts:106-133`,
    `runAgent.ts:297-300`). Parent approvals must not leak into a child, or
    delegation becomes privilege escalation.

18. **A passive verifier scoped by attribution.** Before a mutating tool
    touches a file, the harness snapshots that file's current diagnostics
    (`tools/FileEditTool/FileEditTool.ts:425`,
    `tools/FileWriteTool/FileWriteTool.ts:247`); afterwards only findings *not*
    in the baseline are injected, and the baseline advances so each is reported
    once (`services/diagnosticTracking.ts:266-279`). Findings for files the
    agent never touched are dropped entirely (`services/diagnosticTracking.ts:210-212`).
    The question becomes "did *your* edit break something", which is the only
    one the model can act on — reporting all 400 pre-existing warnings in a
    real repository teaches it to ignore the channel.

19. **Feedback is withheld when the agent cannot act on it.** Diagnostics are
    not injected at all unless the agent has a shell tool: "diagnostics are
    only useful if the agent has the Bash tool to act on them"
    (`utils/attachments.ts:2857-2862`).

20. **Memory is defined by non-derivability.** Four types — user, feedback,
    project, reference — under one rule: code patterns, architecture, git
    history and file structure are recoverable with grep and git and must NOT
    be saved (`memdir/memoryTypes.ts:1-12`). `feedback` must record
    confirmations as well as corrections, because "if you only save
    corrections, you will avoid past mistakes but drift away from approaches
    the user has already validated, and may grow overly cautious"
    (`memdir/memoryTypes.ts:60`).

21. **Staleness is expressed in human units and carried with the content.**
    "47 days ago", not an ISO timestamp, because "models are poor at date
    arithmetic — a raw ISO timestamp doesn't trigger staleness reasoning"
    (`memdir/memoryAge.ts:10-14`); the caveat is suppressed for memories under
    two days old, where it would be noise (`memdir/memoryAge.ts:33-41`). The
    motivating failure is precise: a stale `file:line` citation makes a wrong
    claim sound *more* authoritative, not less.

22. **Planning is a permission mode, and the plan is a file.** Plan mode makes
    mutating tools unavailable rather than discouraged; exiting it takes no
    plan parameter because the model has already written the plan to disk, so
    approval refers to a durable artifact
    (`tools/ExitPlanModeTool/prompt.ts:1-10`). Exploration fans out to three
    read-only agents by default (`utils/planModeV2.ts:31-43`).

23. **Tool prompts are compiled, and reference other tools by constant**
    (`tools/BashTool/prompt.ts:275-369`). The shell tool's description is
    assembled at runtime from feature flags, sandbox configuration, git
    settings, and timeout values — and every mention of a sibling tool is an
    imported name constant, never a string literal. Renaming a tool cannot
    leave a stale instruction behind. This is reference 06's prompt-as-code
    applied one level down, to the *tool descriptions* rather than the system
    prompt.

## Failure modes this design guards against

| Failure mode | Guard |
|---|---|
| Irreversible action taken without consent | Per-input `isDestructive` + `checkPermissions` + 27 hook events + a permission dialog that offers scoped rules |
| Model-supplied privileged fields | Internal-only fields stripped from model input before use, as defence in depth behind a strict schema (`toolExecution.ts:756-773`) |
| Prompt-cache thrash | Volatile lists moved to attachments; per-user paths normalised; built-in tools sorted as a contiguous prefix ahead of MCP tools (`tools.ts:354-366`) |
| Context blowup from one huge result | Per-tool cap → disk persistence with preview + path; per-message aggregate cap |
| Context blowup from many tool schemas | Deferred loading above a 10%-of-context threshold, recovered via a search tool |
| Capability library growth | Skill index budgeted at ~1% of context with staged degradation to names-only |
| A tool author forgetting a safety method | `buildTool()` fail-closed defaults in one place |
| Untrusted skills injecting shell | Expansion refused by *source* for MCP skills; nested skill dirs gitignore-filtered (`skills/loadSkillsDir.ts:371-396, 886-897`) |
| New frontmatter capability silently auto-allowed | Safe-property **allowlist** — unknown properties force a permission prompt (`tools/SkillTool/SkillTool.ts:875-933`) |
| Subagent recursion / privilege creep | Explicit disallowed-tool sets per agent class (`constants/tools.ts:36-112`) |
| Blanket-denied tools still visible to the model | Deny rules filter the tool pool *before* assembly, not at call time (`tools.ts:262-269`) |
| Silent security downgrade | An explicitly-enabled sandbox that cannot run reports a human-readable reason instead of quietly disabling (`utils/sandbox/sandbox-adapter.ts:550-556`) |

## Known sharp edges (inherited, do better)

- **The `Tool` type is ~40 members, most of them rendering.** It works
  because there is one host application; it is not a portable tool contract.
  If you copy it, split the *semantic* half (schema, validate, permissions,
  read-only, concurrency, result cap) from the *presentation* half, or every
  headless consumer inherits a React dependency.
- **The Bash surface is ~8,500 lines of permission and command-parsing logic
  for one tool.** That is the real cost of admitting an open-world escape
  hatch — shell command semantics must be re-derived to decide whether a
  string is read-only. A narrower action space (reference 10) is cheaper than
  parsing an arbitrarily general one, and the parser is a standing
  correctness risk: anything it mis-parses is mis-permissioned.
- **Nested skill discovery fails open outside a git repository**
  (`skills/loadSkillsDir.ts:886-897`). The gitignore filter is a convenience,
  and the source says so; the permission dialog is the only real boundary. If
  you adopt the pattern without the dialog, you have adopted an injection
  path.
- **Correctness of `isConcurrencySafe` / `isReadOnly` is unverified.** Both
  are self-reported by the tool and directly drive parallel execution and
  auto-allow. A tool that lies about being read-only gets both.
- **`excludedCommands` is explicitly not a security boundary**
  (`tools/BashTool/shouldUseSandbox.ts:18-20`), yet reads like one to a user
  configuring it. Convenience filters that look like controls should be named
  so they cannot be mistaken for controls.

## What transfers to your domain

The tool-call gauntlet, the fail-closed `buildTool` defaults, overflow-to-disk
result handling, and progressive disclosure of both skills and tool schemas
are domain-independent and reusable as-is — they are properties of *any*
agent whose capability surface is larger than its context window.

The permission system transfers only with its premise: it is worth this much
machinery because a human is present and irreversible actions are on the
table. In an unattended pipeline, the same budget is better spent on a
verifier (reference 03) — Articraft's compiler gate does a job no permission
dialog can do, and Claude Code's dialog does a job no compiler can do. The
general rule the two case studies jointly establish:

> **An agent needs exactly one authority that can say "no" to a finished
> attempt.** If the domain admits mechanical verification, make it the
> verifier. If it does not, make it the human — and then spend your
> engineering on making the question you ask them cheap, scoped, and rare.

## Coverage

Read and verified in this pass (non-UI source; the terminal renderer,
components and ink layer are out of scope by design):

| Subsystem | Files | Lands in |
|---|---|---|
| Turn loop | `query.ts`, `QueryEngine.ts`, `query/stopHooks.ts`, `services/tools/StreamingToolExecutor.ts` | ref 01 |
| Tool contract & pool | `Tool.ts`, `tools.ts`, `constants/tools.ts`, `constants/toolLimits.ts` | ref 02 |
| Tool-call lifecycle | `services/tools/toolExecution.ts`, `utils/toolResultStorage.ts` | ref 02 |
| Sandbox | `utils/sandbox/sandbox-adapter.ts`, `tools/BashTool/{prompt,shouldUseSandbox,bashPermissions}` | ref 04 |
| Context management | `services/compact/{compact,autoCompact,microCompact}.ts`, `utils/attachments.ts` | ref 05, 06 |
| Prompt assembly | `constants/{prompts,systemPromptSections}.ts` | ref 06 |
| Cost | `cost-tracker.ts`, `utils/modelCost.ts`, `query/tokenBudget.ts` | ref 07 |
| Orchestration & subagents | `tools/AgentTool/*`, `constants/tools.ts`, `sdk-tools.d.ts` | ref 09 |
| Advisory verification | `services/diagnosticTracking.ts`, `services/lsp/passiveFeedback.ts`, `skills/bundled/verify.ts` | ref 03 |
| State & transcripts | `utils/sessionStorage.ts` | ref 08 |
| Memory | `memdir/*`, `utils/claudemd.ts`, `tools/AgentTool/agentMemory.ts` | ref 14 |
| Planning | `utils/planModeV2.ts`, `tools/EnterPlanModeTool/*`, `tools/ExitPlanModeTool/*` | ref 15 |
| Skills & disclosure | `skills/loadSkillsDir.ts`, `skills/bundledSkills.ts`, `tools/SkillTool/*`, `utils/toolSearch.ts`, `tools/ToolSearchTool/*` | ref 11 |
| Hooks | `utils/hooks.ts`, `services/tools/toolHooks.ts`, `entrypoints/sdk/coreSchemas.ts` | ref 12 |
| Permissions | `utils/permissions/*`, `types/permissions.ts` | ref 13 |
| Tool surface | all 42 directories under `src/tools/`, plus `sdk-tools.d.ts` | [tool catalogue](claude-code-tool-catalog.md) |

Every `file:line` citation in this library is checked against the source by an
automated pass — 0 unresolved at time of writing.

**Deliberately not distilled:** the terminal UI (`components/`, `ink/`,
`screens/`, `hooks/`, `vim/`, `native-ts/` — ~136k lines), the MCP client
internals, plugin marketplace management, auth, and the installer. These are
either product-specific or well-covered elsewhere; none change the agent
architecture this library teaches.
