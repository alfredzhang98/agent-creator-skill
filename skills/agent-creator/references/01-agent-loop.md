# 01. The Agent Loop

**Maps to:** LLM/Policy · Tools · Executor · Evaluator/Verifier · Guardrails · Cost · State/Context · Memory · **Distilled from:** Articraft `agent/harness.py`, `agent/models.py`, `agent/payload_preview.py`

## Why this module exists

An agent is a while-loop around an LLM, and every hard problem lives in that loop: models declare success without verifying their work, emit empty responses, pass malformed tool arguments, re-fetch the same documents until context explodes, and burn money past any budget. The loop is where you enforce the one invariant that makes the agent trustworthy: **the run succeeds only when the latest artifact revision passed an external check** — never on the model's say-so. It is also where cost caps, stall detection, corrective feedback injection, and checkpoint/restore compatibility must live, because no other layer sees every turn. Get this module right and provider quirks, tool bugs, and model misbehavior all degrade into recoverable in-band feedback instead of crashed or lying runs.

## How Articraft implements it

### Turn loop with dual counters

`run()` (`agent/harness.py:1151-1518`) seeds the workspace from a scaffold if empty (`agent/harness.py:1157`), then loops on `completed_turns < self.max_turns` (`agent/harness.py:1187`), tracking `llm_calls` separately from turns. Each iteration: optional provider `prepare_next_request` hook, pre-call cost check, LLM call, codec-based extraction of text/tool_calls/usage, assistant-message append, no-action handling, finish-attempt gate, tool batch execution, guidance injection. Critically, `completed_turns` increments on no-action turns too (`agent/harness.py:1385`), so a model emitting only hidden reasoning cannot loop forever under the cap. Exhaustion returns `MAX_TURNS` with a message that says whether the latest revision was ever successfully verified (`agent/harness.py:1497-1518`).

### Success gate: fresh-verify invariant

Visible text with zero tool calls is treated as a finish attempt (`agent/harness.py:1090-1093`). `_handle_finish_attempt` (`agent/harness.py:1122-1149`) checks a revision comparison: every successful mutating tool bumps an edit-revision counter (`agent/harness.py:994-995`), and the verifier records the revision at its last success. If the latest revision is unverified, the harness appends a `<compile_required>` user reminder and the loop *continues* — the model cannot declare victory over stale code. If fresh, it returns `AgentResult(success=True)` with the cached report and final artifact (`agent/harness.py:1095-1120`).

### No-action detection with two-stage escalation

A turn with no tool calls and blank text increments a streak (`agent/harness.py:1383-1386`). Below `NO_ACTION_ESCALATION_STREAK=2`, a gentle state-aware nudge is injected — `<final_response_required>` if verified, else `<compile_required>` (`agent/harness.py:422-475`). At streak 2, a hard escalation names concrete tool names and embeds provider diagnostics (status, incomplete reason, response shape; `agent/harness.py:719-733`). The run aborts at streak 3 **only if the escalation flag is set** (`agent/harness.py:1387-1417`) — guaranteeing the final warning was actually delivered before spend is written off. Any productive turn resets both streak and flag (`agent/harness.py:1429-1430`).

### Verify tool: revision-keyed caching and failure streaks

`_execute_compile_model` (`agent/harness.py:566-612`) short-circuits when code is unchanged since the last success, returning a re-rendered cached signal bundle without recompiling (`agent/harness.py:569-576`) — redundant defensive verify calls are free. Genuine attempts increment an attempt counter (`agent/harness.py:578`) and run under a concurrency semaphore (`agent/harness.py:551-558`). Every failure computes a signature and increments a consecutive-failure counter — the signature comparison flags identical repeats, while the counter resets only on success (`agent/harness_compile.py:129-143`) — and both counter and signature are fed into the provider's `prepare_next_request` (`agent/harness.py:1213-1219`) so the provider can adapt (e.g. escalate reasoning effort).

### Tool dispatch: errors are data, not exceptions

`_execute_tool` (`agent/harness.py:812-1029`) converts every failure mode into a structured error ToolResult returned as a tool message: malformed JSON args, non-dict args, unexpected params on zero-arg tools (`agent/harness.py:881-901`), and validation errors rendered as `Missing required: [...] Invalid values: [...] Provided: [...]` (`agent/harness.py:996-1014`). Domain preflight guards catch classic edit-tool failure modes with exact corrective instructions (`agent/harness.py:916-970`). Batches run in parallel only when the provider codec explicitly marks them parallelizable — currently Gemini only (`agent/harness.py:1031-1070`); everything else serializes because file edits are order-dependent.

### Guidance injection with signature dedup

After each tool batch, three injectors run in fixed order (`agent/harness.py:1476-1490`), appending targeted user-role guidance wrapped in XML-ish tags. Per-run signature sets (`agent/harness.py:369-384`) guarantee the same lecture is never injected twice for the same underlying issue, keeping context clean when the model keeps hitting the same wall. Verifier failure signals are appended as separate structured user messages (`agent/harness.py:541-549`).

### Shared typed signal vocabulary

All evaluator feedback flows through one frozen dataclass, `CompileSignal` (`agent/models.py:36-81`): severity, kind, machine code, summary, details, blocking flag, source, group, check name, dedupe key — grouped into a `CompileSignalBundle` (`agent/models.py:84-109`). Symmetric `to_dict`/`from_dict` let the same objects cross the compiler subprocess boundary, render into prompts, persist into trajectory records, and rehydrate on rerun. `from_dict` is total — every field has a defensive default — so old or partial records never throw. `TerminateReason` (`agent/models.py:9-16`) is a closed StrEnum of exactly five exit reasons; `AgentResult` (`agent/models.py:19-33`) packages outcome plus counters.

### Cost: dual cap checks and persistent ledger

Cost accumulates per turn via `CostTracker.add_turn(usage)` (`agent/harness.py:1337`) plus billed maintenance/compaction events (`agent/harness.py:636-669`). The cap is checked twice per iteration: pre-call after compaction billing (`agent/harness.py:1264-1286`) — compaction itself costs tokens and must not buy one more full generation — and post-usage (`agent/harness.py:1349-1372`). Every terminal path calls `_persist_cost_tracking` (`agent/harness.py:621-629`), writing `cost.json` next to the artifact so spend is recorded even on error.

### Checkpoint-safe state mirroring

Loop state lives in helper objects, but agents are pickled mid-run and old checkpoints predate the helpers. Each `_ensure_X()` lazily rebuilds a helper from flat legacy attributes on the agent (`agent/harness.py:304-346, 358-386`), and `_sync_*_legacy_attrs` copies helper state back onto flat attrs after every mutation (`agent/harness.py:348-356`) — a bidirectional mirror that keeps both old and new serialized forms restorable without migration.

### Context economy: prompt-cache keys and retrieval dedup

`build_openai_prompt_cache_settings` derives the cache key purely from sha256 hashes of the static prefix — system prompt, docs, and canonically-sorted tool schemas (`agent/harness.py:138-182`) — so identical batch runs share cache while any prompt/tool change auto-invalidates. Retrieval tool output is compressed at dispatch: content already returned this run is replaced with a visible skip sentinel keyed by stable id (`agent/harness.py:756-810, 992-993`), and the seen-set is re-seeded from restored conversations so resumed runs neither re-send nor mis-skip.

### Dry-run payload preview

`build_provider_payload_preview` (`agent/payload_preview.py:24-83`) reconstructs the exact first request a real run would send — system prompt, docs context, first-turn messages, tool schemas, cache settings — then instantiates the provider with `dry_run=True` (client creation skipped) and returns `llm.build_request_preview(...)`. The agent's real interface is the bytes it sends; making them inspectable offline without credentials enables golden-file tests of prompt assembly.

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Gate success on fresh external verification of the latest revision, not model self-report | Revision-counter comparison is a cheap, unfakeable freshness proof; forces verify after every edit | Burns turns when the model summarizes right after an edit; every mutating tool must bump the counter or the gate deadlocks into MAX_TURNS |
| Return tool failures as structured error results, never raise | Model self-corrects when told exactly what was wrong; harness exceptions become in-band signal | A confused model can loop on bad calls; only max_turns and cost cap bound it |
| Graduated no-action response: nudge → hard escalation → flag-gated abort | Empty responses are usually transient; abort only after the strong warning was provably delivered | Up to 3 wasted turns per silent stretch; thresholds are global, not budget-adaptive |
| Cache verify results by revision; surface failure streak + signature to the provider | Verification is the expensive step and models re-run it defensively; streaks enable adaptive provider behavior | Cache trusts the revision counter completely — out-of-band file edits serve stale success |
| Check cost cap both pre-call (after compaction billing) and post-usage; persist ledger on every exit | Compaction spend alone can cross the cap and buy an extra generation; ledger stays accurate on abort | Cap is soft — the in-flight call that crosses it completes and is billed |
| Inject corrective guidance as tagged user-role messages, deduped by signature | User-role is the only channel steering all providers mid-conversation; dedup stops repeated lectures filling context | Synthetic messages pollute transcripts (filter before fine-tuning); ignored guidance never repeats for the same signature |
| Parallel tool execution only when the codec proves the batch independent | File edits are order-dependent; opt-in parallelism captures speedup without racing | Read-only batches from most providers run slower than necessary |
| Drop empty assistant messages instead of appending | Several provider APIs reject conversations containing empty assistant turns | Transcript no longer records the call happened; only usage/cost does |
| Mirror helper-object state onto flat attrs bidirectionally | Pickled mid-run agents predating the refactor must stay restorable without migration | Every mutation must remember to sync or checkpoints silently go stale |
| One frozen serializable signal type with total `from_dict` for all evaluator feedback | Signals cross subprocess, prompt, persistence, and rerun boundaries; one vocabulary prevents schema drift | Corrupt records degrade quietly to defaults instead of failing loudly |
| Thread `dry_run=True` through every provider constructor | The request payload is the agent's real interface; offline inspection enables golden-file testing | Preview path can drift from the live path if untested |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| `MAX_CONSECUTIVE_NO_ACTION_TURNS` | 3 (`agent/harness.py:62`) | Abort after 3 empty responses — but only if the hard escalation was already delivered |
| `NO_ACTION_ESCALATION_STREAK` | 2 (`agent/harness.py:63`) | Second empty turn switches from gentle nudge to hard demand naming exact tools + diagnostics |
| `_FIND_EXAMPLES_SKIPPED_CONTENT` | `"{Skipped: full content already returned earlier in this run.}"` (`agent/harness.py:61`) | Visible sentinel: model learns content was deliberately elided, not lost |
| Prompt cache key cap | 64 chars total, 16-char prefix, `ac1:{digest}` (`agent/harness.py:167-174`) | OpenAI `prompt_cache_key` length limit; prefix dropped if combined key would exceed it |
| Cache retention default | `"24h"` for supported model families, else provider default (`agent/harness.py:119-135`) | Extended retention only where the model family supports it; env-overridable |
| `max_turns` | per-model default via `resolve_max_turns` (`agent/harness.py:252, 1187`) | Hard iteration cap; no-action turns count, so silence cannot evade it |
| `max_cost_usd` | `None` default, env-settable (`agent/harness.py:203, 254`) | USD cap checked pre-call and post-usage; returns `COST_LIMIT` with exact overage |
| `TerminateReason` values | `GOAL_COMPLETE, CODE_VALID, MAX_TURNS, COST_LIMIT, ERROR` (`agent/models.py:9-16`) | Closed enum of the only five exits; persisted in results and trajectory records |
| Cost ledger filename | `cost.json` next to the artifact (`agent/harness.py:625`) | Written on every terminal path, including error/abort |

## Reusable pattern

```python
"""Generic verified-artifact agent loop (stdlib only).

Core invariant: the run succeeds ONLY when the latest artifact revision
passed the external verifier. The model cannot declare victory by text.
"""
import asyncio, hashlib, json
from dataclasses import dataclass
from enum import Enum

ESCALATE_AT = 2   # streak where nudge becomes a hard, tool-naming demand
ABORT_AT = 3      # streak where run aborts -- only after escalation was sent


class Exit(str, Enum):
    VALID = "VALID"; MAX_TURNS = "MAX_TURNS"; COST_LIMIT = "COST_LIMIT"; ERROR = "ERROR"


@dataclass(frozen=True)
class Signal:
    """One vocabulary for ALL verifier feedback. Crosses subprocess,
    prompt, and persistence boundaries; from_dict is TOTAL (never raises)."""
    severity: str = "warning"       # failure | warning | note
    code: str = "UNKNOWN"
    summary: str = ""
    blocking: bool = False

    def to_dict(self):
        return {"severity": self.severity, "code": self.code,
                "summary": self.summary, "blocking": self.blocking}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("severity", "warning"), d.get("code", "UNKNOWN"),
                   d.get("summary", ""), bool(d.get("blocking", False)))


class Verifier:
    """Revision bookkeeping: mutations bump `revision`; success pins it."""
    def __init__(self, check_fn):
        self.check_fn = check_fn
        self.revision = 0
        self.verified_revision = -1
        self.cached_report = None
        self.failure_streak = 0
        self.last_failure_sig = None

    def mark_mutated(self):
        self.revision += 1

    def latest_is_fresh(self):
        return self.cached_report is not None and self.verified_revision == self.revision

    async def run(self, workspace):
        if self.latest_is_fresh():
            return self.cached_report              # no-op verify: free
        report = await self.check_fn(workspace)    # subprocess / tests / build
        if report["ok"]:
            self.cached_report, self.verified_revision = report, self.revision
            self.failure_streak, self.last_failure_sig = 0, None
        else:
            sig = hashlib.sha256(report["summary"].encode()).hexdigest()[:16]
            self.failure_streak += 1               # every consecutive failure counts
            self.last_failure_sig = sig            # sig flags repeats; feed both to provider
        return report


def user_msg(text):
    return {"role": "user", "content": text}


class AgentLoop:
    def __init__(self, llm, codec, tools, verifier, guidance, cost,
                 max_turns=40, max_cost=None):
        self.llm, self.codec, self.tools = llm, codec, tools
        self.verifier, self.guidance, self.cost = verifier, guidance, cost
        self.max_turns, self.max_cost = max_turns, max_cost
        self.system, self.workspace = "...", "..."

    async def run(self, task):
        conv = self.build_first_turn(task)         # task + domain docs context
        completed, streak, escalated = 0, 0, False

        while completed < self.max_turns:
            # 1) Optional provider pre-flight: compaction + failure adaptation.
            prep = getattr(self.llm, "prepare_next_request", None)
            if callable(prep):
                events = await prep(messages=conv, completed_turns=completed,
                                    failure_streak=self.verifier.failure_streak,
                                    last_failure_sig=self.verifier.last_failure_sig)
                self.cost.bill_events(events)      # compaction costs money too
            if self.over_cost():
                return self.finish(Exit.COST_LIMIT)          # check BEFORE the call...

            resp = await self.llm.generate(self.system, conv, self.tools.schemas())
            text, calls, usage = self.codec.extract(resp)
            self.cost.add_turn(usage)
            if self.codec.has_payload(resp):       # NEVER append empty assistant msgs
                conv.append(self.codec.assistant_message(resp))
            if self.over_cost():
                return self.finish(Exit.COST_LIMIT)          # ...and after

            # 2) No-action turn still burns a turn: nudge -> escalate -> abort.
            if not calls and not text.strip():
                completed += 1; streak += 1
                if streak >= ABORT_AT and escalated:
                    return self.finish(Exit.ERROR, diag=self.codec.diagnostics(resp))
                escalated |= self.inject_no_action_reminder(conv, streak)
                continue
            streak, escalated = 0, False
            completed += 1

            # 3) Finish attempt = text with zero tool calls; gate on freshness.
            if text.strip() and not calls:
                if self.verifier.latest_is_fresh():
                    return self.finish(Exit.VALID, report=self.verifier.cached_report)
                conv.append(user_msg("<verify_required>Your latest edits are not "
                                     "verified. Run the verify tool first."
                                     "</verify_required>"))
                continue

            # 4) Dispatch sequentially unless the codec proves independence.
            for call in calls:
                result = await self.dispatch(call)
                if result.get("ok") and call["name"] != "verify":
                    self.verifier.mark_mutated()   # every mutating success bumps
                conv.append({"role": "tool", "tool_call_id": call["id"],
                             "name": call["name"], "content": json.dumps(result)})

            # 5) Targeted corrective guidance, signature-deduped per run.
            self.guidance.maybe_inject(conv, calls)

        return self.finish(Exit.MAX_TURNS,
                           note="" if self.verifier.latest_is_fresh()
                           else "no fresh verification of latest edits")

    async def dispatch(self, call):
        """Errors are DATA returned to the model, never raised exceptions."""
        try:
            raw = call.get("arguments", "{}")
            args = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(args, dict):
                return {"ok": False, "error": "arguments must be a JSON object"}
            if call["name"] == "verify":
                return await self.verifier.run(self.workspace)
            return await self.tools.invoke(call["name"], args)  # schema-validated;
        except Exception as e:                     # validation errors render as
            return {"ok": False,                   # "Missing required: [...]"-style text
                    "error": f"{type(e).__name__}: {e}"}

    def finish(self, reason, **extra):
        self.cost.persist()                        # ledger on EVERY exit path
        return {"reason": reason.value, **extra}

# Checkpoint safety: if agents are pickled mid-run, mirror every helper field
# to flat attrs after each mutation, and lazily rebuild helpers from those
# flat attrs on restore -- old serialized forms stay loadable forever.
#
# Dry-run preview: give every provider client a dry_run flag that skips client
# creation but keeps payload assembly, plus build_request_preview(system,
# messages, tools) -- then golden-test the exact bytes you send.
```

## Pitfalls

- **Success-by-assertion**: without the fresh-verify gate, models return confident answers over broken artifacts. Use a revision counter bumped by every mutating tool — a timestamp or "did verify ever succeed" check passes stale successes.
- **Silent infinite loops**: no-action turns must still increment the turn counter (`agent/harness.py:1385`), or a model emitting only hidden reasoning loops forever.
- **Premature abort**: never kill a run on a no-action streak without confirming the hard escalation message was actually delivered first (`agent/harness.py:1387-1390`).
- **Checkpoint drift**: refactoring loop state into helper objects breaks previously pickled agents; the ensure/sync mirror (`agent/harness.py:304-391`) exists for exactly this. Forgetting one sync call silently corrupts the next checkpoint.
- **Post-only cost checks**: compaction/maintenance spend can cross the cap and then pay for one more full generation — check before the call too (`agent/harness.py:1264-1286`).
- **Empty assistant messages**: appending them causes hard API errors on the next request with some providers — filter on payload keys (`agent/harness.py:1322-1331`).
- **Raising on bad tool args** removes the model's chance to self-correct; but note there is no per-tool-error streak counter, so repeated identical bad calls are bounded only by max_turns and cost.
- **Unchecked parallel tool execution** races file edits; make parallelism opt-in per provider codec (`agent/harness.py:1035`).
- **Retrieval duplication**: dedupe repeated large documents by stable key with a visible skip sentinel, and re-seed the seen-set from the conversation on resume (`agent/harness.py:756-779`) or restored runs mis-skip.
- **Impure cache keys**: prompt cache keys must be pure functions of the static prefix including canonically-sorted tool schemas (`agent/harness.py:138-182`); anything per-run in the key destroys batch cache hits, while omitting tool schemas serves stale cache after tool changes.
- **Partial deserializers**: signal/record `from_dict` must be total with defensive defaults (`agent/models.py:64-81`), or old trajectory records become unloadable after schema evolution.

## Checklist

- [ ] Define a closed exit-reason enum; every loop return path maps to exactly one value
- [ ] Gate finish attempts (text + zero tool calls) on a revision-fresh external verification; inject a reminder and continue when stale
- [ ] Bump the revision counter in every mutating tool's success path — audit this list when adding tools
- [ ] Cache verify results by revision so redundant verify calls are free; track failure streak + failure signature and pass both to the provider hook
- [ ] Count no-action turns toward the turn cap; implement nudge → hard escalation (naming exact tools + provider diagnostics) → flag-gated abort
- [ ] Return all tool failures (JSON parse, schema validation, preflight guards) as structured error results with exact corrective text
- [ ] Serialize tool batches by default; allow parallelism only when the provider codec proves independence
- [ ] Dedupe injected guidance by signature per run; wrap guidance in distinctive tags in user-role messages
- [ ] Check the cost cap pre-call (after billing compaction) and post-usage; persist the cost ledger on every exit path
- [ ] Drop assistant messages with no payload keys instead of appending them
- [ ] Define one frozen, serializable signal type with total `from_dict` for all evaluator feedback
- [ ] Thread `dry_run` through provider constructors and expose a payload-preview entry point; golden-test the assembled request
- [ ] Derive prompt cache keys from content hashes of the static prefix (system prompt + docs + sorted tool schemas) only
- [ ] If agents checkpoint mid-run, mirror helper-object state to flat attrs after every mutation and rebuild helpers lazily on restore
