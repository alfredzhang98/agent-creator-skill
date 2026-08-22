# 00. Agent Anatomy — the module map

**Maps to:** everything · **Distilled from:** Articraft (full codebase) · Claude Code 2.1.88 (non-UI source)

This is the orientation document: what the parts of an agent are, how they
connect at runtime, and which reference covers each. Read it once before any
other reference.

## The runtime flow

```
input / task
   ↓
LLM (the brain)  ←——————————————————————————————┐
   ↓                                             │
decide: final answer? call tools? keep planning? │
   ↓                                             │
Tools / APIs / code / search                     │
   ↓                                             │
results (successes AND structured errors)        │
   ↓                                             │
update state / memory / cost ledger              │
   └────────────── next turn ────────────────────┘
   ↓ (only when the verifier confirms fresh success, or a guardrail fires)
final output + persisted trajectory
```

The loop is not a free-running cycle. Three gates shape it:

- **Entry gate** — cost cap and turn cap are checked *before* each LLM call.
- **Action gate** — tool calls are validated, bound to harness-chosen targets
  (never model-chosen file paths), and executed with errors returned as data.
- **Exit gate** — a finish attempt (text, no tool calls) only succeeds if the
  latest artifact revision has a fresh successful verification; otherwise the
  loop injects a reminder and continues.

## Module table

| Module | Role | Reference | Articraft implementation |
|---|---|---|---|
| **LLM / Policy** | Understanding, reasoning, decisions | `05-providers.md` | 7 backends behind one Protocol: OpenAI (HTTP/WS), Anthropic, Gemini, DeepSeek, DashScope, OpenRouter, Codex CLI |
| **System Prompt** | Identity, goals, rules, tool contracts | `06-prompts.md` | Markdown sections compiled to 6 per-provider artifacts, content-addressed at run time |
| **Tools** | The action surface | `02-tools.md` | read_file, write_file/write_code, replace, apply_patch, compile_model, probe_model, find_examples |
| **Planner** | Decompose goals, get sign-off, track progress | `15-planning.md` | Articraft: implicit, shaped by guidance injection. Claude Code: an enforced read-only plan mode, a plan file, an approval gate, a one-task-in-progress todo list |
| **Executor** | Run steps, dispatch tools | `01-agent-loop.md` | The harness turn loop: validate → bind → execute → feed back |
| **State / Context** | Where the task is now | `08-state-persistence.md` | Staging dir per run, revision counters, conversation history, compile-freshness tracking |
| **Memory** | Cross-task knowledge | `14-memory.md` | Articraft: record library with lineage, BM25 retrieval as a tool. Claude Code: four non-derivable memory types, manifest → cheap-model recall, staleness carried with content |
| **Skills** | Reusable specialised abilities | `11-skills-progressive-disclosure.md`, `10-action-space-sdk.md` | Articraft: the domain SDK as the action vocabulary. Claude Code: skills as progressively disclosed capability — index always, body on invoke, directory on demand |
| **Evaluator / Verifier** | Is it correct? Is it done? | `03-evaluator-verifier.md` | Articraft (**gating**): compile + QC → typed signals → one primary issue. Claude Code (**advisory**): baseline-diffed diagnostics attributed to the agent's own edits, reported once, never blocking |
| **Guardrails** | Permissions, safety, limits | `13-permission-and-consent.md`, `04-sandboxed-execution.md` | Articraft: harness-bound file targets, anti-reward-hacking language, tolerance clamps. Claude Code: a numbered consent ladder with bypass-immune steps, plus an OS sandbox whose purpose is to *reduce* how often you must ask |
| **Extension points** | Third-party code in your lifecycle | `12-hooks-and-extension.md` | 27 named lifecycle events; exit-code + JSON reply protocol; unconditional trust gate; blocking is feedback, not termination |
| **Cost / Billing** | Spend metering + caps | `07-cost-guardrails.md` | Per-model pricing tables (cached/uncached/write tiers), dual ledgers, hard USD cap, max-turns |

## How the modules interlock (the load-bearing edges)

These cross-module couplings are where production agents differ from demos:

1. **Verifier ↔ Loop.** The loop's success gate *is* the verifier: a
   monotonically increasing edit-revision counter (bumped by every successful
   mutating tool) compared against the revision at the last successful
   verify. This one integer comparison is what prevents success-by-assertion.

2. **Verifier ↔ Providers.** The consecutive-failure streak and failure
   signature are plumbed *into* the provider layer, where compaction policy
   uses "stuck on the same failure" as a trigger: a compile-failure plateau
   fills the context window with low-value noise, so being stuck justifies
   summarising earlier than pressure alone would.

3. **Tools ↔ Prompts.** Tool surfaces are per-provider (models perform best
   with the tool names/shapes they were RL-trained on), which forces the
   system prompt to be per-provider too — hence prompt-as-code with a build
   matrix rather than one prompt string.

4. **Cost ↔ Loop.** The cap is checked twice per iteration because context
   compaction itself costs tokens: a post-response-only check lets a
   compaction push spend past the cap and then pay for one more generation.

5. **Action space ↔ everything.** Constraining the model to a small
   declarative vocabulary (instead of the raw target format) is what makes
   eager validation, entity-named error messages, derived collision geometry,
   and mechanical QC possible at all. The action space is the root decision;
   the verifier's quality is downstream of it. The converse is measurable, and
   reference 13 measures it: an open-world shell costs thousands of lines of
   permission and command-parsing logic just to decide whether a string is
   read-only.

6. **Verifier ↔ Consent.** These are not two modules but two answers to one
   question — *what can refuse a finished attempt?* If the domain admits a
   mechanical check, the verifier owns the exit and consent is a formality. If
   it does not, the human owns the exit and the verifier drops to advisory,
   where it must be attribution-scoped and report-once or it gets ignored.
   Exactly one authority must be able to say no; zero is the failure mode.

7. **Sandbox ↔ Consent.** Isolation is not a tax on capability, it is what pays
   for autonomy: a command that provably runs inside the sandbox needs no
   prompt. Every class of action you can safely isolate is a class of question
   you never have to ask again.

8. **Disclosure ↔ everything.** What the model can *see* is a layer above what
   it can *do*. Skills, deferred tool schemas and the memory index all trade a
   round-trip for prefix cost, and the governing constraint is the same in all
   three: nothing volatile may live in a cached prefix. Reference 06 has the
   measured cost of breaking that rule; reference 11 has the pattern.

## Sizing guide — what a minimal agent actually needs

| Agent size | Required | Skippable |
|---|---|---|
| Prototype (single model, single task) | Loop, 2-3 tools, verifier (gating or advisory), cost cap, OS-isolated sandbox for generated code | Providers layer, memory, planning, hooks, disclosure, batch orchestration, prompt build matrix |
| Production single-domain | + persistence/traces, prompt-as-code, consent ladder if a human is in the loop, guardrail battery | Multi-provider (keep a seam), memory, hooks |
| Interactive / human-in-the-loop | + consent ladder, plan mode, progress tracking, advisory verifier wired to your real checkers | Multi-provider, fleet orchestration |
| Platform / fleet | + provider abstraction, compaction policy, derived-run flows, batch concurrency, hooks, progressive disclosure, memory | — |

Never skippable at any size: **something that can refuse a finished attempt**
(verifier or human), **cost cap**, **OS-level isolation of generated code**,
**tool errors as data**.

## Vocabulary used across the references

- **Turn** — one LLM call plus the tool executions it triggers. No-action
  turns still count toward the cap.
- **Revision** — a monotonic counter of artifact mutations; the unit of
  verify-freshness.
- **Signal / signal bundle** — a typed verification outcome
  (severity ∈ {failure, warning, note}) plus rendering metadata; the shared
  currency between verifier, loop, persistence, and reruns.
- **Staging-then-promote** — all in-flight artifacts live in a per-run
  scratch dir; only verified successes are committed to the library; failures
  keep the staging dir for debugging.
- **Guidance injection / attachment** — deterministic detectors that append
  synthetic user-role messages when the model hits a known failure pattern,
  deduplicated by content signature so the same lecture is never delivered
  twice. At scale this becomes a closed vocabulary of typed attachment kinds.
- **Gating vs advisory verification** — a gating verifier refuses the exit; an
  advisory one reports and lets the run finish. Advisory verification must be
  attribution-scoped (only problems this agent caused) and report-once.
- **Progressive disclosure** — index always in context, payload on request,
  reference files on demand. Applies to skills, tool schemas and memory alike.
- **Bypass-immune** — a permission decision that a "skip all prompts" mode
  cannot override: explicit denies, calls that inherently need a human, and
  safety checks.
