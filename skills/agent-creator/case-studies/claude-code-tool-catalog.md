# Claude Code — annotated tool catalogue

Companion to [case study 02](claude-code.md). Claude Code 2.1.88 ships **42
tool directories** (`src/tools/`); this catalogue records, for each one worth
copying, *what it is*, *what problem it solves*, and *the transferable design
idea* — the part you can steal even if your agent never touches a repository.

Read [reference 02](../references/02-tools.md) for the generic tool-layer
pattern and [reference 11](../references/11-skills-progressive-disclosure.md)
for the disclosure machinery that decides which of these the model can even
see on turn 1.

**Steal-first shortlist** — the six that pay off in almost any agent:
`Read` · `Edit` (exact-string) · `Bash` (+ sandbox) · `TodoWrite` ·
`AskUserQuestion` · `ToolSearch`.

---

## A. Files and retrieval

| Tool | What it is | The idea worth stealing |
|---|---|---|
| **Read** ★ | Read a file by absolute path with `offset`/`limit`; renders images/PDFs/notebooks natively | **Self-bounding tools opt out of global truncation.** Declares `maxResultSizeChars: Infinity` *because* persisting a read result would create a Read→file→Read loop, and it already caps at 2,000 lines / 25,000 tokens (`Tool.ts:457-466`, `FileReadTool/prompt.ts:10`, `limits.ts:18`). Cross-cutting policies need documented, reasoned exemptions. |
| **Edit** ★ | Exact-string replacement; fails unless `old_string` is unique, requires a prior Read | **Make the dangerous operation impossible to get subtly wrong.** "The edit will FAIL if `old_string` is not unique" (`FileEditTool/prompt.ts:26`) converts *edited the wrong occurrence* — a silent corruption — into a loud, self-correctable error, with `replace_all` as the explicit escape hatch. Read-before-edit is enforced, not requested: "This tool will error if you attempt an edit without reading the file" (`FileEditTool/prompt.ts:4-5`). `strict: true` additionally tightens API-side schema adherence (`FileEditTool.ts:90`). |
| **Write** | Create or overwrite a whole file | **State the precondition in the description *and* enforce it.** "If this is an existing file, you MUST use Read first… This tool will fail if you did not read the file first" (`FileWriteTool/prompt.ts:6-7, 14`). Prompt text and runtime check agree, so the model is never punished for following the description — nor rewarded for ignoring it. |
| **Glob** / **Grep** | Filename patterns; ripgrep content search with `head_limit` | **Give search its own, tighter result cap.** Grep declares 20,000 chars against the 100,000 default (`GrepTool.ts:164`) and defaults to 250 matches (`GrepTool.ts:108`): search output is the classic context-flooder, and a cap on the *tool* beats a reminder in the prompt. Both are droppable when the shell ships fast embedded equivalents (`tools.ts:198-201`) — tool sets are environment-dependent. |
| **NotebookEdit** | Cell-level `.ipynb` editing | **A format that breaks under line-based editing gets its own tool.** Cheaper than teaching the model to hand-patch JSON. Deferred by default — narrow tools should not cost turn-1 tokens. |
| **LSP** | `goToDefinition`, `findReferences`, `hover`, `documentSymbol` | **Wrap an existing semantic index instead of asking the model to grep for meaning.** One tool, an operation enum, real answers. The generic move: when your domain has an authoritative index, expose it. |

## B. Executing things

| Tool | What it is | The idea worth stealing |
|---|---|---|
| **Bash** ★ | Shell command with timeout, `run_in_background`, and an OS sandbox | **Isolation buys autonomy.** `autoAllowBashIfSandboxed` defaults **true** — inside the sandbox, no permission prompt (`sandbox-adapter.ts:471`). The prompt also teaches the model to recognise sandbox-caused failures and immediately retry with an explicit override rather than asking (`BashTool/prompt.ts:228-256`). Second idea: the description is **compiled at runtime** from sandbox config, feature flags, and sibling tools' *name constants*, so renames cannot leave stale instructions (`BashTool/prompt.ts:275-369`). Third: it actively steers *away from itself* — "use Read not cat, Edit not sed" — because a general tool used for a specific job destroys reviewability. Sobering cost: ~8,500 of ~12,400 lines in this tool are permission/security/validation. |
| **PowerShell** | Windows-native sibling, enabled by platform check | **Platform variants are separate tools behind one capability, not `if` branches inside one.** Each gets the idioms its ecosystem expects. |
| **REPL** | A VM that wraps the primitive tools; when enabled, the primitives are *hidden* from direct use (`tools.ts:312-323`) | **A wrapper tool must remove what it wraps.** Otherwise the model sees two routes to the same effect and the permission story forks. `isTransparentWrapper()` lets it render inner calls as if they were native (`Tool.ts:528-533`). |

## C. Talking to the human

| Tool | What it is | The idea worth stealing |
|---|---|---|
| **AskUserQuestion** ★ | Multiple-choice question(s); "Other" is always available; optional per-option `preview` | **Make asking cheap and structured, so the agent asks at the right time instead of guessing or stalling.** Free-text questions are expensive to answer and ambiguous to parse; 2–4 labelled options with a recommended-first convention are not. `requiresUserInteraction()` marks it so the harness knows this call blocks on a human (`AskUserQuestionTool.tsx:155`). The `preview` field carries an artifact to compare, not just a label — the UI switches to side-by-side layout. |
| **EnterPlanMode** / **ExitPlanMode** | Enter a read-only planning mode; exit by submitting a plan for approval | **Model the approval gate as a state machine with tool-shaped transitions.** Plan mode is a *permission mode*, so read-only-ness is enforced, not requested. ExitPlanMode reads the plan **from a file the model already wrote** rather than taking it as a parameter (`ExitPlanModeV2Tool`) — the artifact exists on disk before approval, so approval refers to something durable. |
| **TodoWrite** ★ | A structured checklist for the current session | **Externalise the plan into inspectable state, and say precisely when *not* to use it.** The prompt spends as much space on "skip for single trivial tasks" as on when to use it (`TodoWriteTool/prompt.ts:17-25`) — an always-on tool that fires on trivia trains the model to perform bookkeeping instead of work. Note it renders to a panel, not the transcript: `renderToolResultMessage` is *omitted* so the result costs no transcript space (`Tool.ts:561-566`). |
| **TaskCreate / TaskGet / TaskUpdate / TaskList** | The durable, multi-agent successor to TodoWrite | **When state outlives the turn and is shared, split one write-everything tool into CRUD verbs.** Each is separately permissionable, separately deferrable, and separately assignable to a teammate. |
| **Brief** | The channel whose content the user actually reads | **When output has more than one audience, make the primary channel a tool.** Text outside it is detail-view only. Never deferred (`ToolSearchTool/prompt.ts:83-94`) — the model must see the visibility contract before its first response. |

## D. Delegation and concurrency

| Tool | What it is | The idea worth stealing |
|---|---|---|
| **Agent** (Task) | Spawn a subagent of a named type with its own tool allowlist | **Delegation is capability-scoped by class, declaratively.** `ALL_AGENT_DISALLOWED_TOOLS`, `CUSTOM_AGENT_DISALLOWED_TOOLS`, `ASYNC_AGENT_ALLOWED_TOOLS`, `COORDINATOR_MODE_ALLOWED_TOOLS` are four explicit sets in one file with the *reasons* written next to them — recursion prevention, main-thread-only abstractions, singleton conflicts (`constants/tools.ts:36-112`). Second idea: the agent *list* was moved out of the tool description into an attachment after it cost ~10.2% of fleet cache-creation tokens (`AgentTool/prompt.ts:48-59`). |
| **TaskOutput** | Read output/logs from a background task or agent | **Long-running work returns a handle, not a blocked turn.** Aliased to its old names (`AgentOutputTool`, `BashOutputTool`) so historical transcripts still resolve (`TaskOutputTool.tsx:150`). |
| **TaskStop** | Kill a running background task | **Anything spawnable must be killable by the model**, or a runaway job needs a human. |
| **SendMessage** / **ListPeers** | Address another agent by name | **Give agents addresses.** Turns a tree of one-shot subagents into a graph of resumable ones. |
| **TeamCreate / TeamDelete** | Create/disband a team, 1:1 with a task list | **Bind the coordination structure to the work structure** so there is no "who owns this task" ambiguity. |
| **Workflow** | Run a deterministic multi-agent script (fan-out, pipeline, barriers) | **When orchestration should be deterministic, move it out of the model.** Loops and conditionals belong in a script; judgement belongs in the agents the script spawns. |

## E. Context economy

| Tool | What it is | The idea worth stealing |
|---|---|---|
| **ToolSearch** ★ | Fetch full JSON schemas for deferred tools by name or keyword | **Progressive disclosure of the tool surface itself.** 24 of 42 tool directories ship as names only, plus every MCP tool; schemas arrive on request (`ToolSearchTool/prompt.ts:62-108`). Auto-enables when deferrable schemas exceed 10% of the context window (`utils/toolSearch.ts:45-49`). Never deferred itself, and neither are tools whose prompt carries a turn-1 contract. **Recorded negative result:** rendering each deferred tool's one-line hint showed no A/B benefit, so the announcement is the bare name (`ToolSearchTool/prompt.ts:110-117`). |
| **Skill** ★ | Invoke a named capability; the body loads only on invoke | **The same ladder one level up: index → body → the skill's own directory.** Index budgeted at ~1% of context; typed refusal codes; auto-allow gated by a safe-property *allowlist* so new frontmatter fields default to asking. Full treatment in [reference 11](../references/11-skills-progressive-disclosure.md). |
| **CtxInspect** / **Snip** | Inspect and prune the agent's own context (experimental) | **Context is state the agent can be given tools over**, not just a passive buffer. |

## F. The outside world

| Tool | What it is | The idea worth stealing |
|---|---|---|
| **WebFetch** | Fetch a URL and extract content | **Mark it `isOpenWorld` and defer it.** Fetched content is untrusted input; the flag lets the harness treat it accordingly. |
| **WebSearch** | Search the web | Same class; deferred by default. |
| **MCP tools** | Third-party tools from connected servers | **Always deferred, unless the server opts out** via `_meta['anthropic/alwaysLoad']` (`Tool.ts:443-449`). Deny rules prefixed with the server name strip *all* of a server's tools before the model sees them (`tools.ts:262-269`) — filter the pool, do not reject at call time. And sort built-ins as a contiguous prefix ahead of MCP tools so an added MCP tool cannot interleave and bust the cache (`tools.ts:354-366`). |
| **ListMcpResources / ReadMcpResource** | Enumerate and read MCP resources | **Separate "what exists" from "give me this one"** so discovery is cheap and reading is explicit. |

## G. Environment and lifecycle

| Tool | What it is | The idea worth stealing |
|---|---|---|
| **EnterWorktree / ExitWorktree** | Create an isolated git worktree and switch into it, then leave | **Let the agent create its own isolation.** Both defer; only Exit declares `isDestructive(input)` (`ExitWorktreeTool.ts:168`) — entering is cheap, leaving can discard work, and only the second fact needs to reach the permission layer. |
| **Config** | Read/write the agent's own settings | **Self-configuration is a tool, hence permissioned and audited** — not a side effect buried in some other call. |
| **CronCreate / CronDelete / CronList** | Schedule recurring or one-shot prompts; durable jobs persist to `.claude/scheduled_tasks.json` | **Give the agent a future tense.** Split create/delete/list so "list" stays read-only and auto-allowable. |
| **RemoteTrigger** | Manage scheduled remote agents through an API | **"Auth is handled in-process — the token never reaches the shell."** If a tool needs a secret, the tool holds it; never route credentials through a general execution tool. |
| **Sleep** / **Monitor** | Wait, or stream events from a background process | **Waiting is a tool so it can be interrupted, budgeted, and discouraged.** The Bash prompt spends a whole section forbidding sleep-polling and pointing at completion notifications instead (`BashTool/prompt.ts:310-328`). |
| **SyntheticOutput** | Return the final response as structured JSON | **A typed exit.** When a run must produce a machine-readable result, the terminal turn is a tool call with a schema, not prose the caller has to parse. |
| **PushNotification / SendUserFile** | Reach the user out-of-band; hand over a file | **Delivery is an explicit, permissioned act**, distinct from producing the content. |

---

## Cross-cutting patterns visible only in aggregate

1. **Every tool answers the same six semantic questions** — `isReadOnly`,
   `isConcurrencySafe`, `isDestructive`, `isOpenWorld`, `isEnabled`,
   `interruptBehavior` — *as functions of the input*
   (`Tool.ts:402-437`). Parallelism, permission strictness, interrupt
   behaviour, and UI collapsing are all derived from those answers, so the
   harness never needs a table of tool names.

2. **Defaults are fail-closed and live in one function.** `buildTool()`
   supplies `isConcurrencySafe → false`, `isReadOnly → false`,
   `isDestructive → false`, `toAutoClassifierInput → ''`
   (`Tool.ts:757-792`). Forgetting a method is safe; there is no
   `?.() ?? default` anywhere else.

3. **Result size is a per-tool declaration, not a global constant.**
   `Infinity` (Read) · 20k (Grep) · 30k (Bash) · 100k (everything else),
   clamped by a 50k default and a 200k per-message aggregate
   (`constants/toolLimits.ts`). Overflow persists to disk and returns a
   2,000-byte preview plus a path — relocation, not truncation.

4. **Tool descriptions reference sibling tools by imported constant**, never
   by string literal, and are assembled at runtime from configuration. Tool
   prompts are code (reference 06), one level below the system prompt.

5. **What the model can see is filtered before assembly, not at call time.**
   Deny rules strip tools from the pool; mode filters remove wrapped
   primitives; `isEnabled()` drops the rest (`tools.ts:271-327`). A tool the
   model cannot use should not occupy tokens telling it so.

6. **Ordering is a cache decision.** Built-ins sorted as a contiguous prefix,
   MCP tools sorted after them, dedup preserving insertion order
   (`tools.ts:345-367`) — because the server places a cache breakpoint after
   the last built-in.
