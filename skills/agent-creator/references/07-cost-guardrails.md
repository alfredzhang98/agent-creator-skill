# 07. Cost Metering & Guardrails

**Maps to:** Cost · Guardrails · Executor · State/Context · LLM/Policy · **Distilled from:** Articraft `agent/cost.py`, `agent/harness.py`, `agent/defaults.py`, `agent/run_context.py`, `agent/single_run.py` · Claude Code 2.1.88 `src/cost-tracker.ts`, `src/utils/modelCost.ts`, `src/query/tokenBudget.ts`

## Why this module exists

An autonomous agent loop is an unbounded spending machine: every turn buys tokens, context compaction buys more, and a stuck loop can burn a real budget before anyone notices. You need three things wired together: (1) accurate USD metering that mirrors how each provider actually bills — cache reads, cache writes, long-context price tiers — not a naive `tokens * rate`; (2) a hard per-run budget cap that aborts the loop with a typed, auditable failure instead of an exception; (3) a companion turn-count limit so cheap models don't loop forever under the same dollar cap. The metering must also survive every exit path as a persisted audit file, because the one run you need to post-mortem is the one that died weirdly.

## How Articraft implements it

### Pricing tables: flat dicts, fixed key vocabulary, snapshotted into output
Each (provider, model family) gets a plain `dict[str, float]` of USD-per-million-token rates (`agent/cost.py:15-127`). Required keys: `input_uncached`, `input_cached`, `output`. Optional: `input_cache_write` / `input_cache_write_1h` (Anthropic 5-minute/1-hour cache-write premiums), and a long-prompt tier via `prompt_tier_threshold_tokens` plus `*_above_threshold` overrides (Gemini 3 Pro doubles input rates — output goes 12 to 18 $/Mtok — above 200k tokens, `agent/cost.py:27-35`; GPT-5.x tiers at 272k, `agent/cost.py:44-74`; Qwen 3.6 at 256k, `agent/cost.py:93-111`). Families that share pricing are aliased by plain assignment (`agent/cost.py:42`, `agent/cost.py:84-85`), so one table edit updates every alias. The table actually used is embedded in the persisted `cost.json` (`agent/cost.py:347`) so historical runs stay auditable after prices change.

### One cost function over a normalized usage schema
`calculate_cost` (`agent/cost.py:175-232`) takes a provider-normalized usage dict — keys like `prompt_tokens`, `cached_tokens`, `cache_creation_input_tokens` (with 5m/1h detail), `candidates_tokens` for output — produced upstream by each provider's message codec (`agent/harness.py:677-678`). One vendor's vocabulary (Gemini's) is the cross-provider lingua franca. Mechanics:

- `regular_uncached = max(0, prompt - cached - cache_creation)`.
- Tier selection is **all-or-nothing**: if `prompt_tokens > threshold`, the entire prompt *and* output are priced at above-threshold rates (`agent/cost.py:188-203`), matching how Google/OpenAI actually invoice long-context requests.
- Cache-write rate falls back through: tiered write key → base write key → uncached rate (`agent/cost.py:204-207`); 1h-write falls back to base write (`agent/cost.py:208`); unattributed cache-creation tokens bill at the 5m rate (`agent/cost.py:209-213`).
- Cache-write cost folds into the `input_uncached_cost` bucket (`agent/cost.py:215-217`); everything divides by 1,000,000.
- Returns a `CostBreakdown` dataclass (`agent/cost.py:130-146`).

### Dual-ledger tracker: productive turns vs maintenance overhead
`CostTracker` (`agent/cost.py:239-359`) keeps a `total_breakdown` for normal turns, a **separate** `maintenance_breakdown` for overhead (context compaction, provider-close flushes), a per-turn breakdown list, and a maintenance event log. `add_turn` (`agent/cost.py:250-262`) prices one turn and accumulates field-wise. `add_maintenance_event` (`agent/cost.py:274-310`) whitelist-copies only int-typed usage keys, prices with the same table, and logs the annotated event. `all_in_total_breakdown` (`agent/cost.py:312-338`) sums both ledgers on demand — **this is what the budget cap checks**, so overhead spend is separable for analytics but can never silently blow the budget. Serialization rounds costs to 8 decimals only at write time; accumulation stays full precision (`agent/cost.py:159-165`).

### Pricing resolution: provider enum gate + ordered prefix predicates
Tiny predicates (`model_id.strip().lower().startswith(prefix)`, `agent/cost.py:362-427`) feed `pricing_for_provider_model` (`agent/cost.py:430-469`), an if-chain gated on a normalized provider enum so `gpt-5.4` under a non-OpenAI provider never matches. Order encodes specificity: `gemini-3.5-flash` before the generic `'flash'` substring check (`agent/cost.py:447-450`); GPT 5.6 before 5.5 before 5.4. Unknown pairs return `None`; the harness then skips creating a tracker entirely (`agent/harness.py:253-257`) and `_current_total_cost_usd` returns 0.0 (`agent/harness.py:631-634`) — the run proceeds untracked and the cap becomes inert (see Pitfalls).

### Budget parsing and three-level resolution
`parse_max_cost_usd` (`agent/cost.py:472-484`) accepts anything `str()`-able, returns `None` for None/blank, raises `ValueError` on non-numeric or `<= 0` — a zero budget is invalid; disable via absence. Precedence: CLI `--max-cost-usd` flag (`runner_cli.py:164-168`, parse errors go to stderr and exit code 1, `runner_cli.py:209-216`) > value stored in the parent record's generation params for forks/reruns (`edit.py:246-289`, `rerun.py:131-159`, validated on read via `_optional_max_cost_usd`, `run_context.py:294-298`; a bad stored value yields a failed outcome with exit code 1 in the callers, `edit.py:251-258`, `rerun.py:136-138`) > env var `ARTICRAFT_MAX_COST_USD` (`articraft/config.py:14`, read at `agent/cost.py:487-492`). The resolved budget is persisted into `record.json` generation params (`record_persistence.py:316,349,441,473`) so reruns inherit the original guardrail by default.

### Enforcement: two post-hoc checkpoints per loop iteration, strict `>`
The cap is checked at two sites in the turn loop, both against the all-in total:

1. **Pre-call** (`agent/harness.py:1264-1286`) — immediately after `prepare_next_request` billed any maintenance/compaction events, before paying for another LLM call. Message: `Cost limit exceeded before turn {N}: cumulative $X.XXXXXX exceeded limit $Y.YYYYYY`.
2. **Post-response** (`agent/harness.py:1349-1372`) — right after `cost_tracker.add_turn(usage)` prices the response (`agent/harness.py:1335-1338`). Message: `...after turn N...`.

Both paths persist `cost.json` first, mark the turn failed in the display, and return `AgentResult(success=False, reason=TerminateReason.COST_LIMIT, ...)`. `TerminateReason` is a `StrEnum` `{GOAL_COMPLETE, CODE_VALID, MAX_TURNS, COST_LIMIT, ERROR}` (`agent/models.py:9-16`). Enforcement is post-hoc by design: a run can overshoot by exactly one response/compaction and is never aborted mid-request. The CLI help documents this honestly: "Stops after the first response that pushes cumulative spend above this threshold."

### Maintenance/compaction billing pipeline
`_record_maintenance_event` (`agent/harness.py:636-669`) routes provider maintenance events through the tracker's maintenance ledger, forwards them to the display with `billed_cost` (TypeError fallback for older display signatures), and fires an optional callback whose exceptions are logged, never fatal. Compaction events from `prepare_next_request` get their own display line and callback (`agent/harness.py:1230-1263`). At close, `llm.close()` may return final maintenance events that are also billed (`agent/harness.py:1520-1529`). The TUI adds turn cost via `add_llm_call` and maintenance cost via the maintenance/compaction hooks — one path per billed event, so nothing double-counts (`agent/tui/single_run.py:296-414`).

### Persistence at every exit, surfaced after the run
`_persist_cost_tracking` (`agent/harness.py:621-629`) writes `cost.json` next to the generated artifact and is called at **five** exit sites: success (`agent/harness.py:1105`), both COST_LIMIT checkpoints (`1269`, `1355`), no-action abort (`1403`), and MAX_TURNS fall-through (`1499`). Save failures log warnings, never raise. The run context promotes staging `cost.json` into `revisions/<revision_id>/cost.json` (`run_context.py:371,377`). Post-run, `single_run.py:408-418` reads it back (preferring `all_in_total` over `total`) to log `Total cost: $X.XXXXXX`; a COST_LIMIT result persists a failed record carrying the exact message and exits with code 2 (`single_run.py:397-406`).

### Companion guardrail: per-model-family default max-turns
`DEFAULT_MAX_TURNS = 100`; Gemini 3 Flash gets 250 (`agent/defaults.py:1-21`) — the cheap/fast family gets 2.5x the turn budget, trading turns for per-turn capability. `resolve_max_turns` returns the explicit user value if given, else the family default, and the harness resolves it against the **actual** model id reported by the constructed provider client, not the raw CLI arg (`agent/harness.py:252`), so alias/default resolution happens first. Hitting the limit returns `reason=MAX_TURNS` with a message noting whether compile ever succeeded (`agent/harness.py:1501-1518`).

## Comparative: Claude Code's cost posture

**Unknown pricing estimates and flags, rather than silently disabling.** An
unrecognised model falls back to a default price, emits telemetry, and sets a
session flag (`utils/modelCost.ts:166-172`); the total is then rendered with
"(costs may be inaccurate due to usage of unknown models)"
(`cost-tracker.ts:228-233`). This is the middle path between Articraft's sharp
edge (unknown pricing silently disables the tracker, so a user-set cap is never
enforced) and refusing to run: **estimate, flag, and surface**.

The distinction worth encoding is which failures get which treatment. When a
*safety* property cannot be honoured, refuse loudly — an explicitly requested
sandbox that cannot start returns a human-readable reason rather than running
unsandboxed (`utils/sandbox/sandbox-adapter.ts:550-556`). When only *accuracy*
is at stake, estimate and annotate. Conflating the two gives you either a
brittle agent or an unsafe one.

**Two independent budget systems, at different layers.** A server-side
`task_budget` bounds the whole agentic turn, with `remaining` recomputed
client-side across compaction boundaries — because after a compact the server
sees only the summary and would under-count the spend that was summarised away
(`query.ts:282-291, 504-515`). Separately, a client-side token budget can
*extend* a turn: on reaching the threshold the loop injects a nudge message and
continues rather than stopping, and records a `diminishing_returns` early-stop
signal (`query.ts:1308-1355`). A budget is not only a ceiling; it can also be a
statement about how much effort a task deserves.

**Ledgers are per-model, not just per-session.** Usage is tracked by model with
cache-read and cache-creation tokens broken out separately
(`cost-tracker.ts:71-79, 200-226`), which is the granularity you need to answer
"is the cache actually paying for itself" — the question that motivates most of
reference 06.

**The cheapest guardrail is a cache-hygiene rule.** One interpolated list inside
a cached prompt prefix cost a production fleet a double-digit percentage of its
cache-creation tokens — reference 06 has the measurement and the fix. No budget
cap recovers that spend; only the structural discipline does. Cost control has
two halves, and the structural half is the larger one.

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Pricing tables hardcoded in source; used table snapshotted into persisted `cost.json` | Price changes are code-reviewed and versioned; every historical run stays auditable after tables change | Price updates need a release; no runtime override for negotiated rates |
| One provider-agnostic cost function over a normalized usage schema (one vendor's key names as lingua franca) | Adding a provider = usage normalization + one table; a single function handles Anthropic cache-writes, OpenAI/Gemini tiers, DashScope/DeepSeek alike | Lossy buckets (cache-write folded into uncached-input cost); schema inherits one vendor's vocabulary |
| Post-hoc cap enforcement at two checkpoints per iteration, strict `>` | Never aborts an in-flight request; needs no cost prediction; pre-call check catches compaction spend before paying for another full call | Guaranteed overshoot of up to one response's cost (documented in CLI help) |
| Dual ledgers (turns vs maintenance) but the cap checks the all-in sum | Overhead spend is separable for analytics yet cannot silently blow the budget | Duplicated field-wise accumulation code; two totals (`total` vs `all_in_total`) to explain |
| Unknown (provider, model) pricing → `None` → no tracker, run proceeds | New/experimental models usable immediately without a pricing-table gate | Cap silently inert for unpriced models — no protection, no warning |
| Budget precedence: explicit arg > stored on parent record > env var; resolved value written back into the record | Reruns/forks reproduce the original guardrail by default; env var gives a fleet default; per-invocation override still wins | Three sources of truth to debug; stored values must be re-validated on read |
| Long-prompt tier is all-or-nothing on `prompt_tokens` (reprices prompt AND output) | Mirrors real Google/OpenAI long-context invoicing; tracked cost matches the bill | One token over the threshold doubles the whole request's tracked rate — correct, but surprising if assumed marginal |
| Per-family default max-turns resolved against the provider-reported actual model id | Cheap models iterate more per dollar; resolving after client construction gives aliased/default models the right budget | Turn count is a crude spend proxy; substring family checks need maintenance |
| Cost persisted at every terminal path; display/callback fan-out is best-effort and never raises | `cost.json` survives any exit; observability failures must not kill a paid run | Swallowed callback/persistence exceptions can hide integration bugs |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| `DEFAULT_MAX_TURNS` | `100` (`agent/defaults.py:3`) | Turn guardrail for all models without a family override |
| `GEMINI_3_FLASH_DEFAULT_MAX_TURNS` | `250` (`agent/defaults.py:4`) | Cheap fast family gets 2.5x turns to offset weaker per-turn capability |
| Budget env var | `ARTICRAFT_MAX_COST_USD` (`articraft/config.py:14`) | Fleet-wide default; lowest precedence in the resolution chain |
| Anthropic Opus rate structure | `5.00 / 0.50 / 6.25 / 10.00 / 25.00` $/Mtok (`agent/cost.py:76-85`) | The pattern: 10x cache-read discount, 1.25x 5m-write premium, 2x 1h-write premium — same ratios across Sonnet/Haiku (`agent/cost.py:113-127`) |
| Gemini 3 Pro tier threshold | `200_000` tokens; rates roughly double above (`agent/cost.py:27-35`) | Long-context tier: whole request repriced past the threshold |
| GPT-5.x tier threshold | `272_000` tokens (`agent/cost.py:44-74`) | OpenAI long-context tier; only 5.6-sol carries cache-write rates |
| Qwen 3.6 tier threshold | `256_000` tokens (`agent/cost.py:93-111`) | Same tier mechanism reused; cached rate = uncached (no cache discount) |
| Per-million divisor | `1_000_000` (`agent/cost.py:217-219`) | All rates are USD per million tokens |
| Serialization rounding | `round(x, 8)` (`agent/cost.py:161-164`) | Round only at write time; accumulate at full precision |
| Display precision | `$%.6f` (`agent/cost.py:264-272`, `agent/harness.py:1276,1362`) | Six decimals in summaries and the abort message |
| Budget validity | float, strictly `> 0`; None/blank = disabled (`agent/cost.py:472-484`) | Zero/negative budgets rejected loudly, never treated as "free" or "unlimited" |
| Cap comparison | strict `>` vs all-in total, pre-call + post-response (`agent/harness.py:1264-1266,1349-1352`) | Spending exactly the cap is allowed; abort only after exceeding it |
| COST_LIMIT exit | failed record + `exit_code=2` (`agent/models.py:15`, `single_run.py:397-406`) | Typed reason, exact `$X exceeded limit $Y` message, nonzero exit for scripts |

## Reusable pattern

```python
"""Cost metering + budget guardrail for an LLM agent loop. Stdlib only."""
import json, os
from dataclasses import dataclass, field, asdict
from enum import Enum

# 1. Pricing registry: $/Mtok, fixed key vocabulary. Alias families by assignment.
PRICING = {
    ("provider_a", "model-x"): {
        "input_uncached": 5.00, "input_cached": 0.50, "output": 25.00,
        "input_cache_write": 6.25, "input_cache_write_1h": 10.00,   # optional
        # Optional all-or-nothing long-prompt tier:
        # "prompt_tier_threshold_tokens": 200_000,
        # "input_uncached_above_threshold": 10.00, "output_above_threshold": 50.00,
    },
}

def pricing_for(provider: str, model_id: str) -> dict | None:
    mid = model_id.strip().lower()
    for (prov, prefix), table in PRICING.items():   # order: most specific first
        if prov == provider and mid.startswith(prefix):
            return table
    return None   # caller MUST warn/fail if a budget is set but pricing is unknown

@dataclass
class Breakdown:
    prompt_tokens: int = 0; cached_tokens: int = 0; cache_write_tokens: int = 0
    output_tokens: int = 0
    input_cost: float = 0.0; cache_hit_cost: float = 0.0; output_cost: float = 0.0
    @property
    def total_cost(self): return self.input_cost + self.cache_hit_cost + self.output_cost
    def add(self, o):                                  # field-wise accumulation only
        for k in vars(o): setattr(self, k, getattr(self, k) + getattr(o, k))

def calculate_cost(usage: dict, p: dict) -> Breakdown:
    """usage: normalized dict {prompt, cached, cache_write, cache_write_1h, output}."""
    prompt, cached = usage.get("prompt", 0), usage.get("cached", 0)
    write, write_1h = usage.get("cache_write", 0), usage.get("cache_write_1h", 0)
    output = usage.get("output", 0)
    above = prompt > p.get("prompt_tier_threshold_tokens", float("inf"))
    def rate(key):                                     # tiered -> base fallback
        return p.get(f"{key}_above_threshold", p[key]) if above else p[key]
    uncached = max(0, prompt - cached - write - write_1h)
    write_rate = p.get("input_cache_write", p["input_uncached"])
    write_1h_rate = p.get("input_cache_write_1h", write_rate)
    return Breakdown(
        prompt_tokens=prompt, cached_tokens=cached,
        cache_write_tokens=write + write_1h, output_tokens=output,
        input_cost=(uncached * rate("input_uncached")
                    + write * write_rate + write_1h * write_1h_rate) / 1e6,
        cache_hit_cost=cached * rate("input_cached") / 1e6,
        output_cost=output * rate("output") / 1e6,
    )

class CostTracker:
    """Dual ledger: productive turns vs overhead (compaction, close flushes)."""
    def __init__(self, model_id: str, pricing: dict):
        self.model_id, self.pricing = model_id, pricing
        self.turns_total, self.overhead_total = Breakdown(), Breakdown()
        self.per_turn: list[Breakdown] = []; self.overhead_events: list[dict] = []
    def add_turn(self, usage: dict) -> Breakdown:
        b = calculate_cost(usage, self.pricing)
        self.per_turn.append(b); self.turns_total.add(b); return b
    def add_overhead_event(self, event: dict) -> Breakdown:
        usage = {k: v for k, v in event.get("usage", {}).items() if isinstance(v, int)}
        b = calculate_cost(usage, self.pricing)
        self.overhead_total.add(b)
        self.overhead_events.append({**event, "cost_usd": b.total_cost}); return b
    def all_in_total_usd(self) -> float:               # <- the cap checks THIS
        return self.turns_total.total_cost + self.overhead_total.total_cost
    def save_json(self, path: str) -> None:            # round ONLY at serialization
        r = lambda b: {k: round(v, 8) if isinstance(v, float) else v
                       for k, v in asdict(b).items()}
        json.dump({"model_id": self.model_id, "pricing": self.pricing,
                   "total": r(self.turns_total), "overhead_total": r(self.overhead_total),
                   "all_in_total_usd": round(self.all_in_total_usd(), 8),
                   "per_turn": [r(b) for b in self.per_turn],
                   "overhead_events": self.overhead_events},
                  open(path, "w"), indent=2)

# 2. Budget resolution: explicit arg > stored on parent artifact > env var.
def resolve_budget(arg=None, stored=None, env_var="AGENT_MAX_COST_USD"):
    for v in (arg, stored, os.environ.get(env_var)):
        if v is not None and str(v).strip():
            f = float(v)                               # raises loudly on garbage
            if f <= 0: raise ValueError(f"budget must be > 0, got {v!r}")
            return f
    return None                                        # None = uncapped
# Persist the resolved budget into the run record so reruns/forks inherit it.

class Reason(str, Enum):
    GOAL_COMPLETE = "goal_complete"; MAX_TURNS = "max_turns"
    COST_LIMIT = "cost_limit"; ERROR = "error"

# 3. Enforcement: post-hoc, two checkpoints per iteration, strict >.
def run_loop(provider, model_id, goal, cap, max_turns, cost_path):
    table = pricing_for(provider.name, model_id)
    if cap is not None and table is None:
        raise RuntimeError(f"budget set but no pricing for {model_id}")  # no silent inert cap
    tracker = CostTracker(model_id, table) if table else None

    def over_cap():
        return cap is not None and tracker and tracker.all_in_total_usd() > cap
    def abort(when, turn):
        tracker.save_json(cost_path)                   # persist BEFORE returning
        return {"success": False, "reason": Reason.COST_LIMIT,
                "message": (f"Cost limit exceeded {when} turn {turn}: cumulative "
                            f"${tracker.all_in_total_usd():.6f} exceeded limit ${cap:.6f}")}

    for turn in range(1, max_turns + 1):
        req = provider.prepare_next_request()          # may compact context -> overhead spend
        for ev in req.overhead_events:
            if tracker: tracker.add_overhead_event(ev)
        if over_cap(): return abort("before", turn)    # checkpoint 1: pre-call
        response = provider.generate(req)
        if tracker and (usage := provider.extract_usage(response)):
            tracker.add_turn(usage)
        if over_cap(): return abort("after", turn)     # checkpoint 2: post-response
        if goal_reached(response):
            if tracker: tracker.save_json(cost_path)
            return {"success": True, "reason": Reason.GOAL_COMPLETE}
        execute_tools(response)
    for ev in provider.close():                        # close-time flushes are billed too
        if tracker: tracker.add_overhead_event(ev)
    if tracker: tracker.save_json(cost_path)           # every terminal path persists
    return {"success": False, "reason": Reason.MAX_TURNS,
            "message": f"Hit max turns ({max_turns})"}
```

## Pitfalls

- **Silent-cap footgun.** When pricing resolution returns `None`, Articraft creates no tracker and the cap reads 0.0 forever (`agent/harness.py:253-257,631-634`) — a user-set budget is silently unenforced. In a new agent, warn or hard-fail when a budget is set but pricing is unknown.
- **Post-hoc enforcement guarantees overshoot** by up to one response/compaction cost. Document it in the flag's help text, exactly as Articraft's CLI does; don't pretend the cap is exact.
- **Predicate ordering is load-bearing.** Substring/prefix family checks must run most-specific-first (`agent/cost.py:447-450`); a generic `'flash'` substring match placed too early misprices newer family members. Add a new model's predicate above its generic ancestor, and add a resolution test.
- **Cache-write cost folded into the uncached-input bucket** (`agent/cost.py:215-217`) understates the cache-write premium in per-bucket reports. Keep a dedicated bucket if you need clean attribution.
- **Whitelist-by-type drops data silently.** `add_maintenance_event` copies only int-typed usage values (`agent/cost.py:287-289`); a provider emitting float token counts would be billed at zero. Coerce or log-and-count instead.
- **Accumulate field-wise, never reconstruct.** `CostBreakdown.__post_init__` derives fields only when zero (`agent/cost.py:144-146`); rebuilding breakdowns from partial data goes stale. Add fields with `+=` as `CostTracker` does.
- **One threshold prices output too.** The long-prompt tier selects the output rate from `prompt_tokens` (`agent/cost.py:188-199`). If your provider tiers output independently, this single-threshold model is wrong for it.
- **Display double-counting is prevented by convention, not structure.** Turn cost goes through `add_llm_call`; overhead through the maintenance/compaction hooks (`agent/tui/single_run.py:296-414`). The loop must call exactly one path per billed event — assert it in tests.
- **Validate the budget at every entry point** (CLI flag, stored record, env var) and fail loudly (stderr + nonzero exit, or a typed failed outcome). A swallowed `ValueError` turns a typo'd budget into "uncapped".
- **Wire cost persistence to every terminal path.** Articraft calls it at five exit sites (success, both cap checkpoints, error abort, max-turns). A new exit path that forgets loses the audit trail — grep for `return` statements in the loop when you add one.

## Checklist

- [ ] Pricing tables in source code with a fixed key vocabulary (`input_uncached`, `input_cached`, `output`, optional cache-write and tier keys); shared families aliased by assignment
- [ ] Per-provider usage normalization into one schema; a single `calculate_cost` used everywhere
- [ ] Long-prompt tier implemented all-or-nothing if that is how your providers invoice; verified against a real bill
- [ ] Dual ledgers (turns vs overhead) with an `all_in_total` that the cap checks
- [ ] Budget parser rejects `<= 0` and non-numeric loudly; `None`/blank means disabled
- [ ] Resolution chain: explicit arg > value stored on the parent run record > env var; resolved value persisted back into the record
- [ ] Warn or fail when a budget is set but the model has no pricing entry
- [ ] Two cap checkpoints per loop iteration (pre-call after overhead billing, post-response after turn billing), strict `>` comparison
- [ ] Typed terminate reason (`COST_LIMIT`) with a message containing exact cumulative and cap values; nonzero process exit
- [ ] Cost JSON (totals + per-turn + overhead events + pricing snapshot) persisted at every terminal path; rounding only at serialization
- [ ] Close-time provider flushes billed as overhead before the final save
- [ ] Companion max-turns guardrail with per-model-family defaults, resolved against the provider-reported actual model id
- [ ] Tests: pricing resolution ordering, tier boundary (threshold ± 1 token), cap overshoot behavior, and one billed event per display path
