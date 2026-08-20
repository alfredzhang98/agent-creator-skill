# 00. Agent Anatomy — the module map

**Maps to:** everything · **Distilled from:** Articraft (full codebase)

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
| **Planner** | Decompose complex goals | `01-agent-loop.md` | Implicit: the model plans in-context; the harness shapes it via guidance injection and per-failure playbooks |
| **Executor** | Run steps, dispatch tools | `01-agent-loop.md` | The harness turn loop: validate → bind → execute → feed back |
| **State / Context** | Where the task is now | `08-state-persistence.md` | Staging dir per run, revision counters, conversation history, compile-freshness tracking |
| **Memory** | Cross-task knowledge | `08-state-persistence.md`, `02-tools.md` | Record library with lineage (fork/edit), BM25 example retrieval as a tool |
| **Skills** | Reusable specialised abilities | `10-action-space-sdk.md` | The domain SDK: declarative parts/articulations vocabulary + self-test vocabulary, pluggable via SdkProfile |
| **Evaluator / Verifier** | Is it correct? Is it done? | `03-evaluator-verifier.md` | Compile + two-layer QC → typed signal bundle → one primary issue → suggested next steps |
| **Guardrails** | Permissions, safety, limits | `04-sandboxed-execution.md` | OS-isolated sandbox backend for generated code, supervised by parent-enforced timeouts; harness-bound file targets; anti-reward-hacking prompt language; tolerance clamps |
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
   the verifier's quality is downstream of it.

## Sizing guide — what a minimal agent actually needs

| Agent size | Required | Skippable |
|---|---|---|
| Prototype (single model, single task) | Loop, 2-3 tools, verifier, cost cap, OS-isolated sandbox for generated code | Providers layer, memory, batch orchestration, prompt build matrix |
| Production single-domain | + persistence/traces, prompt-as-code, guardrail battery | Multi-provider (keep a seam) |
| Platform / fleet | + provider abstraction, compaction policy, derived-run flows, batch concurrency | — |

Never skippable at any size: **verifier**, **cost cap**, **OS-level isolation
of generated code**, **tool errors as data**.

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
- **Guidance injection** — deterministic detectors that append synthetic
  user-role messages when the model hits a known failure pattern, deduplicated
  by content signature so the same lecture is never delivered twice.
