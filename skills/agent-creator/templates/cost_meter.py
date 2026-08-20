"""Per-token pricing and a hard cost cap enforced between LLM calls.

Distills the usage/cost accounting threaded through
``articraft.agent.harness`` (cap checkpoints, typed cost-limit exit),
``articraft.agent.events`` and ``articraft.agent.record`` (per-turn ledger,
persistence), and the usage normalization each ``articraft.agent.provider``
adapter performs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# $/Mtok, keyed by (provider, model-id prefix).  Most specific prefix wins.
PRICING: dict[tuple[str, str], dict[str, float]] = {
    ("providerA", "model-x"): {
        "input_uncached": 5.00,       # regular prompt tokens
        "input_cached": 0.50,         # prompt tokens served from cache
        "input_cache_write": 6.25,    # optional: tokens written to cache
        "output": 25.00,
    },
}


def pricing_for(provider: str, model_id: str) -> dict[str, float] | None:
    """Prefix match, most specific first.  None => run is UNTRACKED (document it)."""
    mid = model_id.strip().lower()
    matches = [(prefix, table) for (prov, prefix), table in PRICING.items()
               if prov == provider and mid.startswith(prefix)]
    if not matches:
        return None
    return max(matches, key=lambda m: len(m[0]))[1]


@dataclass
class Breakdown:
    input_cost: float = 0.0       # uncached prompt + cache writes
    cache_hit_cost: float = 0.0   # cached prompt tokens
    output_cost: float = 0.0

    @property
    def total(self) -> float:
        return self.input_cost + self.cache_hit_cost + self.output_cost


def calculate_cost(usage: dict[str, int], pricing: dict[str, float]) -> Breakdown:
    """usage is NORMALIZED by the adapter: prompt / cached / cache_write / output."""
    prompt, cached = usage.get("prompt", 0), usage.get("cached", 0)
    written = usage.get("cache_write", 0)
    uncached = max(0, prompt - cached - written)
    write_rate = pricing.get("input_cache_write", pricing["input_uncached"])
    return Breakdown(
        input_cost=(uncached * pricing["input_uncached"] + written * write_rate) / 1e6,
        cache_hit_cost=cached * pricing.get("input_cached", 0.0) / 1e6,
        output_cost=usage.get("output", 0) * pricing["output"] / 1e6,
    )


class CostMeter:
    """Dual ledger: productive turns vs maintenance overhead (compaction etc.).

    Cap semantics (mirrors the harness): the loop calls over_cap() BEFORE every
    LLM call — so the run aborts without spending — and again right after
    accounting a response.  The cap compares all_in_total() (BOTH ledgers) with
    strict '>': a cap of 0.50 allows exactly $0.50.
    """

    def __init__(self, pricing: dict[str, float] | None,
                 max_cost_usd: float | None = None) -> None:
        self.pricing = pricing            # None => untracked; the cap never trips
        self.max_cost_usd = max_cost_usd  # None => uncapped
        self.turn_costs: list[float] = []
        self.turn_usage: list[dict[str, int]] = []
        self.maintenance_costs: list[float] = []
        self.maintenance_events: list[dict[str, Any]] = []

    def add_turn(self, usage: dict[str, int]) -> float:
        if self.pricing is None:
            return 0.0
        cost = calculate_cost(usage, self.pricing).total
        self.turn_costs.append(cost)
        self.turn_usage.append(dict(usage))
        return cost

    def add_maintenance(self, event: str, usage: dict[str, int]) -> float:
        """Compaction / context upkeep: billed separately, capped together."""
        if self.pricing is None:
            return 0.0
        safe = {k: v for k, v in usage.items()   # whitelist int usage fields
                if isinstance(v, int) and not isinstance(v, bool)}
        cost = calculate_cost(safe, self.pricing).total
        self.maintenance_costs.append(cost)
        self.maintenance_events.append({"event": event, "usage": safe, "cost": cost})
        return cost

    def main_total(self) -> float:
        return sum(self.turn_costs)

    def all_in_total(self) -> float:
        return self.main_total() + sum(self.maintenance_costs)

    def over_cap(self) -> bool:
        return self.max_cost_usd is not None and self.all_in_total() > self.max_cost_usd

    def summary_line(self) -> str:
        cap = f" / cap ${self.max_cost_usd:.2f}" if self.max_cost_usd else ""
        return f"cost ${self.all_in_total():.4f}{cap} over {len(self.turn_costs)} turns"

    def save_json(self, path: Path) -> None:
        """Persist on EVERY exit path (success, cap, max-turns, crash handler)."""
        path.write_text(json.dumps({
            "total_usd": round(self.all_in_total(), 6),   # round only at serialization
            "main_usd": round(self.main_total(), 6),
            "maintenance_usd": round(sum(self.maintenance_costs), 6),
            "per_turn_usd": [round(c, 6) for c in self.turn_costs],
            "maintenance_events": self.maintenance_events,
            "pricing": self.pricing,                      # snapshot for provenance
        }, indent=2))


def resolve_budget(explicit: float | None, stored: float | None,
                   env_var: str = "AGENT_MAX_COST_USD") -> float | None:
    """Override cascade: explicit arg > stored on the record > env.  None = uncapped.

    Persist the RESOLVED budget into the run record so reruns/forks inherit it.
    """
    for value in (explicit, stored, os.environ.get(env_var)):
        if value is not None:
            budget = float(value)
            if budget <= 0:
                raise ValueError(f"budget must be positive, got {budget}")
            return budget
    return None
