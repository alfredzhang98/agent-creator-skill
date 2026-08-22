"""The provider seam: one contract over N model backends.

Adapted from Articraft `agent/providers/base.py` and `agent/providers/_shared.py`
(Apache-2.0), with the retry taxonomy extended using findings from Claude Code
2.1.88 `src/services/api/withRetry.ts`.

The seam exists because provider differences are not cosmetic. They differ in
error taxonomy, in what "usage" counts, in how thinking is requested, and in
whether history lives on their side or yours. A wrapper that only swaps a
base URL leaks all of that into the loop.

Three things here are worth more than the transport code:

1. **A normalised usage schema with a stated inclusivity contract.** Getting
   this wrong silently mis-bills rather than failing, which is the worst
   failure mode a meter can have.
2. **Retryability is a function of the caller, not only the status.** A
   background summarisation call and a user-facing turn should not retry an
   overload the same number of times.
3. **A pure compaction decision function**, so "should we compact" is
   testable without a model, a network, or a conversation.
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

Message = dict[str, Any]
ToolSchema = dict[str, Any]

# --------------------------------------------------------------------------
# Errors: what is worth retrying, and for whom
# --------------------------------------------------------------------------

#: Statuses that are transient regardless of who is asking.
TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
#: Overload. Split out because retrying it aggressively from every background
#: caller is how a busy backend gets a thundering herd from your own fleet.
OVERLOADED_STATUS = 529
#: Substrings that mean "transient" when no status code survived the client.
TRANSIENT_FRAGMENTS = (
    "timeout", "timed out", "connection error", "connection reset",
    "connection aborted", "server disconnected", "protocol error",
    "temporarily unavailable", "rate limit", "bad gateway",
    "service unavailable",
)
#: Never retried: retrying an auth or quota failure just delays the real error.
TERMINAL_STATUS = frozenset({400, 401, 403, 404, 405, 413, 422})


def http_status(exc: BaseException) -> int | None:
    for holder in (exc, getattr(exc, "response", None)):
        if holder is None:
            continue
        for attr in ("status_code", "status"):
            value = getattr(holder, attr, None)
            if isinstance(value, int) and 100 <= value <= 599:
                return value
    return None


def retry_after(exc: BaseException) -> float | None:
    """Honour the server's own backoff hint when it gives one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def should_retry(exc: BaseException, *, foreground: bool = True,
                 overload_attempts_left: int = 3) -> bool:
    """Is this worth another attempt?

    *foreground* is the parameter most retry helpers omit and then regret. A
    user-facing turn should ride out an overload; a background summariser,
    title generator or classifier should give up immediately, because N
    background callers retrying in lockstep is a self-inflicted outage.
    """
    if isinstance(exc, (TimeoutError,)) or isinstance(exc, json.JSONDecodeError):
        return True
    status = http_status(exc)
    if status == OVERLOADED_STATUS:
        return foreground and overload_attempts_left > 0
    if status is not None:
        if status in TERMINAL_STATUS:
            return False
        if status in TRANSIENT_STATUS or status >= 500:
            return True
        return False
    message = str(exc).lower()
    return any(fragment in message for fragment in TRANSIENT_FRAGMENTS)


def describe(exc: BaseException) -> str:
    status = http_status(exc)
    label = type(exc).__name__ + (f" (HTTP {status})" if status else "")
    text = str(exc).strip()
    return f"{label}: {text}" if text else label


@dataclass
class RetryPolicy:
    """Full jitter, because synchronised retries are the failure they cause."""

    max_attempts: int = 4
    base_delay_s: float = 0.5
    max_delay_s: float = 20.0
    max_overload_attempts: int = 3
    foreground: bool = True

    def delay_for(self, attempt: int, hint: float | None = None) -> float:
        if hint is not None:
            return min(hint, self.max_delay_s)
        cap = min(self.max_delay_s, self.base_delay_s * (2 ** max(0, attempt - 1)))
        return random.random() * cap


def call_with_retry(
    operation: Callable[[], Any],
    policy: RetryPolicy | None = None,
    *,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Run *operation*, retrying transient failures. Raises the last error.

    Disable the SDK's own retries when you use this. Two retry loops multiply:
    4 attempts here over an SDK default of 10 is 40 requests for one call.
    """
    policy = policy or RetryPolicy()
    overload_left = policy.max_overload_attempts
    attempt = 0
    while True:
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001
            attempt += 1
            if http_status(exc) == OVERLOADED_STATUS:
                overload_left -= 1
            if attempt >= policy.max_attempts or not should_retry(
                exc, foreground=policy.foreground, overload_attempts_left=overload_left
            ):
                raise
            delay = policy.delay_for(attempt, retry_after(exc))
            if on_retry:
                on_retry(attempt, delay, exc)
            sleep(delay)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------

def key_pool(primary_env: str, pool_env: str,
             env: dict[str, str] | None = None) -> list[str]:
    """Read `FOO_API_KEY` plus an optional comma/newline `FOO_API_KEYS` pool."""
    values = os.environ if env is None else env
    keys: list[str] = []
    if (single := (values.get(primary_env) or "").strip()):
        keys.append(single)
    raw = values.get(pool_env) or ""
    keys.extend(t.strip() for t in raw.replace("\n", ",").split(",") if t.strip())
    return list(dict.fromkeys(keys))


def pick_key(keys: list[str], run_seed: int | None = None) -> str | None:
    """Choose ONCE PER RUN, not per request.

    Rotating per request spreads a conversation across backends whose prompt
    caches are separate, so every turn is a cache miss. The saving from
    spreading load is smaller than the cost of losing the cache.
    """
    if not keys:
        return None
    if run_seed is None:
        return random.choice(keys)
    return keys[run_seed % len(keys)]


# --------------------------------------------------------------------------
# Usage: one schema, with the contract written down
# --------------------------------------------------------------------------

#: THE contract, stated because getting it wrong mis-bills silently:
#: `prompt` is the TOTAL input including cached and cache-write tokens.
#: Anthropic reports `input_tokens` EXCLUSIVE of both, so an adapter must add
#: them back. Gemini reports inclusive. If you skip this, `uncached =
#: prompt - cached - cache_write` clamps at zero and you under-bill forever.
USAGE_FIELDS = ("prompt", "cached", "cache_write", "output", "reasoning")


def normalize_usage(raw: dict[str, Any], *, prompt_is_inclusive: bool) -> dict[str, int]:
    """Map a provider's usage onto the canonical schema.

    Set *prompt_is_inclusive* from the provider's documented behaviour, not
    from observation of one response — a conversation with no cache hits looks
    identical under both conventions.
    """
    def n(*names: str) -> int:
        for name in names:
            value = raw.get(name)
            if isinstance(value, bool) or value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return 0

    cached = n("cached", "cached_tokens", "cache_read_input_tokens")
    write = n("cache_write", "cache_creation_input_tokens")
    prompt = n("prompt", "prompt_tokens", "input_tokens")
    if not prompt_is_inclusive:
        prompt += cached + write
    return {
        "prompt": prompt,
        "cached": cached,
        "cache_write": write,
        "output": n("output", "output_tokens", "candidates_tokens"),
        "reasoning": n("reasoning", "reasoning_tokens", "thoughts_token_count"),
    }


@dataclass(frozen=True)
class Pressure:
    """How full the context window is, after one response."""

    prompt_tokens: int
    max_context_tokens: int | None
    cached_tokens: int = 0

    @property
    def ratio(self) -> float | None:
        if not self.max_context_tokens:
            return None
        return self.prompt_tokens / self.max_context_tokens

    @property
    def cache_ratio(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    @property
    def remaining(self) -> int | None:
        if not self.max_context_tokens:
            return None
        return self.max_context_tokens - self.prompt_tokens


# --------------------------------------------------------------------------
# Compaction: a pure decision, so it can be tested without a model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CompactionPolicy:
    """Thresholds for "should we compact before the next call".

    Soft bands pair a pressure level with a failure streak: being stuck on the
    same error fills the window with low-value repetition, so a plateau
    justifies compacting earlier than pressure alone would. The cache-ratio
    guard exists because compacting throws away a warm cache — when most of
    the prompt is cached, wait one more streak before paying that.
    """

    hard_ratio: float = 0.90
    soft_bands: tuple[tuple[float, int], ...] = ((0.85, 3), (0.70, 4), (0.55, 5))
    cache_ratio_guard: float = 0.60
    cooldown_turns: int = 2


@dataclass(frozen=True)
class CompactionDecision:
    compact: bool
    trigger: str = ""
    detail: str = ""


def decide_compaction(
    pressure: Pressure,
    *,
    failure_streak: int = 0,
    turns_since_last: int = 99,
    policy: CompactionPolicy | None = None,
) -> CompactionDecision:
    """Pure. No I/O, no model, no conversation — just the numbers."""
    policy = policy or CompactionPolicy()
    ratio = pressure.ratio
    if ratio is None:
        return CompactionDecision(False, detail="context window size unknown")
    if turns_since_last < policy.cooldown_turns:
        return CompactionDecision(False, "cooldown",
                                  f"compacted {turns_since_last} turns ago")
    if ratio >= policy.hard_ratio:
        return CompactionDecision(True, "pressure", f"ratio {ratio:.2f}")
    warm = pressure.cache_ratio >= policy.cache_ratio_guard
    for band_ratio, needed_streak in policy.soft_bands:
        if ratio >= band_ratio:
            # A warm cache is worth one more attempt before discarding it.
            required = needed_streak + (1 if warm else 0)
            if failure_streak >= required:
                return CompactionDecision(
                    True, "plateau",
                    f"ratio {ratio:.2f}, streak {failure_streak} >= {required}",
                )
            return CompactionDecision(
                False, detail=f"ratio {ratio:.2f}, streak {failure_streak} < {required}")
    return CompactionDecision(False, detail=f"ratio {ratio:.2f} below all bands")


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------

class Provider(Protocol):
    """What the loop needs from a backend, and nothing more.

    A structural Protocol rather than a base class: a fake needs no
    inheritance, which is what makes the loop testable without a network.
    """

    model_id: str

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """-> {text, tool_calls, usage, error}. `error` is a TYPED string."""
        ...

    def preview(self, messages: list[Message], tools: list[ToolSchema]) -> dict[str, Any]:
        """The exact bytes a real call would send, without sending them.

        The request payload is the agent's real interface. Making it
        inspectable offline is what allows golden-file tests of prompt
        assembly, and it costs one flag.
        """
        ...


@dataclass
class ProviderInfo:
    """Declared capabilities. Ask, rather than sniffing a 400 and retrying."""

    name: str
    max_context_tokens: int | None = None
    prompt_usage_is_inclusive: bool = True
    supports_tools: bool = True
    supports_parallel_tool_calls: bool = False
    supports_thinking: bool = False
    #: Provider-native thinking request shape, keyed by effort level. The three
    #: major providers spell this three different ways; a table beats an `if`.
    thinking_shapes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def thinking_for(self, level: str) -> dict[str, Any]:
        return dict(self.thinking_shapes.get(level, {}))


#: The three spellings, so nobody has to rediscover them.
THINKING_SHAPES = {
    "openai":    {"low": {"reasoning": {"effort": "low"}},
                  "high": {"reasoning": {"effort": "high"}}},
    "anthropic": {"low": {"thinking": {"type": "enabled", "budget_tokens": 4096}},
                  "high": {"thinking": {"type": "enabled", "budget_tokens": 16384}}},
    "gemini":    {"low": {"generationConfig": {"thinkingConfig": {"thinkingBudget": 4096}}},
                  "high": {"generationConfig": {"thinkingConfig": {"thinkingBudget": 24576}}}},
}
