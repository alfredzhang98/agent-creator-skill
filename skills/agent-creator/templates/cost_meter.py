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
import warnings
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
    """Longest-prefix match. None => this model is UNPRICED.

    Provider is normalised too: matching it case-sensitively meant a single
    capital letter silently disabled billing, and a disabled meter makes a
    configured cap inert — the worst outcome this module has.
    """
    mid = model_id.strip().lower()
    prov_key = provider.strip().lower()
    matches = [(prefix, table) for (prov, prefix), table in PRICING.items()
               if prov.strip().lower() == prov_key and mid.startswith(prefix)]
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
        # Required, not optional: defaulting to 0.0 billed cached tokens at
        # zero while the sibling keys raised on absence — inconsistent
        # strictness that silently understates the bill.
        cache_hit_cost=cached * pricing["input_cached"] / 1e6,
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
                 max_cost_usd: float | None = None,
                 model_id: str = "",
                 on_untracked: str = "raise") -> None:
        """*on_untracked* decides what an unpriced model means when a cap is set.

        ``raise``  — refuse to start (default). A cap the user set that can
                     never trip is worse than no cap: it reads as protection.
        ``warn``   — proceed, but the caller is told and the summary says so.

        There is no silent option. That was the previous behaviour and it let
        a $300 run report $0.00 against a one-cent cap.
        """
        if pricing is None and max_cost_usd is not None:
            msg = (
                f"No pricing for model {model_id or '<unknown>'}, but a cost cap "
                f"of ${max_cost_usd:.2f} was set. The cap could never trip. Add "
                "the model to PRICING, or pass on_untracked='warn' to accept an "
                "unenforced cap deliberately."
            )
            if on_untracked == "raise":
                raise ValueError(msg)
            warnings.warn(msg, RuntimeWarning, stacklevel=2)
        self.pricing = pricing
        self.model_id = model_id
        self.untracked = pricing is None
        self.max_cost_usd = max_cost_usd  # None => uncapped
        self.turn_costs: list[float] = []
        self.turn_usage: list[dict[str, int]] = []
        self.maintenance_costs: list[float] = []
        self.maintenance_events: list[dict[str, Any]] = []

    def add_turn(self, usage: dict[str, int]) -> float:
        if self.pricing is None:
            self.turn_usage.append(dict(usage))   # still record what was spent
            return 0.0
        cost = calculate_cost(_coerce_usage(usage), self.pricing).total
        self.turn_costs.append(cost)
        self.turn_usage.append(dict(usage))
        return cost

    def add_maintenance(self, event: str, usage: dict[str, int]) -> float:
        """Compaction / context upkeep: billed separately, capped together."""
        if self.pricing is None:
            return 0.0
        # Same coercion as add_turn: the two ledgers previously billed
        # identical usage differently, so $5 could vanish depending on which
        # method the caller happened to use.
        safe = _coerce_usage(usage)
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
        """Persist the ledger. Never raises — it is called from crash handlers.

        Atomic, because a crash mid-write leaves truncated JSON, losing exactly
        the audit file the caller wanted for the post-mortem.
        """
        payload = {
            "model_id": self.model_id,
            "untracked": self.untracked,
            "main_total_usd": round(self.main_total(), 6),
            "all_in_total_usd": round(self.all_in_total(), 6),
            "max_cost_usd": self.max_cost_usd,
            "turns": len(self.turn_costs),
            "turn_usage": self.turn_usage,
            "maintenance": self.maintenance_events,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            warnings.warn(f"could not save cost ledger to {path}: {exc}",
                          RuntimeWarning, stacklevel=2)


def _coerce_usage(usage: dict[str, Any]) -> dict[str, int]:
    """Normalise token counts to ints, dropping nothing silently.

    A provider emitting float counts must not be billed at zero. Values that
    cannot be read as a count are reported rather than discarded — a usage
    field you cannot parse is a billing gap, not a rounding detail.
    """
    out: dict[str, int] = {}
    for k, v in usage.items():
        if isinstance(v, bool) or v is None:
            continue
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            warnings.warn(
                f"usage[{k!r}]={v!r} is not a token count and was not billed",
                RuntimeWarning, stacklevel=3,
            )
    return out


def resolve_budget(explicit: float | None, stored: float | None,
                   env_var: str = "AGENT_MAX_COST_USD") -> float | None:
    """Override cascade: explicit arg > stored on the record > env.  None = uncapped.

    Persist the RESOLVED budget into the run record so reruns/forks inherit it.
    """
    sources = (("argument", explicit), ("record", stored),
               (f"${env_var}", os.environ.get(env_var)))
    for origin, value in sources:
        # A blank env var is routine in CI and shell profiles; treating "" as
        # "set" turned it into a startup crash rather than "unset".
        if value is None or not str(value).strip():
            continue
        try:
            budget = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"cost budget from {origin} is not a number: {value!r}"
            ) from None
        if budget <= 0:
            raise ValueError(f"cost budget from {origin} must be positive, got {budget}")
        return budget
    return None
