# 05. Provider Abstraction (Multi-LLM)

**Maps to:** LLM/Policy · Tools · State/Context · Memory · Guardrails · Cost · **Distilled from:** Articraft `agent/providers/` (base, factory, _shared, compaction_policy, openai, openai_codec, anthropic, gemini, gemini_codec, chat_completions, dashscope, deepseek, openrouter, codex_cli), `articraft/values.py` · Claude Code 2.1.88 `src/services/compact/`, `src/services/api/claude.ts`, `src/query.ts`

## Why this module exists

One agent loop must run unchanged against many LLM backends, and backends disagree about everything that matters: wire format (Responses API vs Messages vs chat-completions vs a CLI subprocess), statefulness (server-stored responses vs stateless), reasoning artifacts (encrypted reasoning items, thinking blocks, opaque thought signatures that must round-trip byte-exact), caching, error taxonomy, and token accounting. Without a hard boundary, every vendor quirk leaks into the loop and every new backend is a harness rewrite. The provider layer converts all of that to one neutral envelope — system prompt + message list + OpenAI-style tool schemas in, `{content, tool_calls, thought_summary, usage, extra_content}` out — and also owns the context lifecycle (pressure measurement, prompt caching, history compaction), because only the provider knows its native history format and token semantics. Cross-cutting machinery (key pools, transient-error classification, jittered retry, usage normalization) is factored into shared modules so a per-provider file encodes only genuine API quirks.

## How Articraft implements it

### The contract is a structural Protocol, not a base class
`ProviderClient` is a `typing.Protocol` (`agent/providers/base.py:124-156`): `model_id`, `build_request_preview` (exact wire payload, zero network — used for dry runs and golden tests), `async prepare_next_request` (pre-turn maintenance; receives `completed_turns`, `consecutive_compile_failure_count`, `last_compile_failure_sig` from the loop), `async generate_with_tools`, `async close`. Telemetry dataclasses define the maintenance envelope: `CompactionEvent` with before/after item and token counts (`base.py:17-49`), `PrepareRequestResult` (`base.py:52-58`), and `build_context_window_pressure` turning canonical usage into `pressure_ratio = prompt_tokens / max_context_tokens` (`base.py:60-121`). Fakes satisfy the contract structurally; nothing is inherited. Every constructor also honors `dry_run=True` (skip client creation, keep all payload assembly), which backs an offline payload-preview entry point (`agent/payload_preview.py:24-83`).

### One canonical vocabulary, with model-id provider inference
`articraft/values.py:6-89` is the single source of truth for cross-cutting enums: `ProviderName` (7 providers) and `ThinkingLevel` (`low/med/high/xhigh/max`, with `'med'→'medium'` aliasing at the wire boundary and a non-raising fallback to the default). `infer_provider_from_model_id` (`articraft/values.py:38-56`) maps model-id prefixes to providers (`gpt-`/`o1`/`o3`/`o4`→openai, `claude-`→anthropic, `qwen`→dashscope, `gemini-`→gemini, `deepseek-`→deepseek, any `/`→openrouter) so users can omit `--provider`. One internal thinking scale is translated per provider: OpenAI `reasoning.effort`, Anthropic adaptive + `output_config.effort`, Gemini numeric budget (2.5 family) or string level (3.x), DeepSeek collapsed to two values, DashScope token budgets (low=16k, medium=32k). Adding a provider is: enum member, prompt-name field, factory branch, thin subclass.

### Factory: frozen config, injectable constructors, credential preflight
A frozen `ProviderConfig` plus a frozen `ProviderConstructors` dataclass of callables (`agent/providers/factory.py:53-214`, `:69-77`) lets tests swap fakes without monkeypatching. `default_model_id` deliberately refuses a default for the CLI-backed provider (`factory.py:96-110`) so a run never silently inherits whatever a local CLI defaults to. `validate_provider_credentials` (`factory.py:177-214`) checks keys — and `shutil.which` for the subprocess provider — before the first turn, so misconfiguration fails in seconds, not after paid turns.

### Shared plumbing: key pools, error classification, full-jitter retry
Every provider reads a singular key env var plus a plural pool (`FOO_API_KEYS`, split on commas and newlines, deduped, primary first); `random_env_key` picks one per client construction (`agent/providers/_shared.py:25-63`) — a fleet of runs spreads across keys while one run stays on one key, keeping server-side caches and stored-response state coherent. Gemini instead builds one client per key and round-robins per request under a lock (`agent/providers/gemini.py:74-82, 633-639`) because its rate limits are per-key and its cache is not keyed to the API key. Error classification is three-tier — exception type, HTTP status (`{408,409,425,429,500,502,503,504}` + all 5xx, never other 4xx), then lowercase message fragments — and `async_retry` sleeps `uniform(0,1) * min(cap, base * 2^(attempt-1))` (full jitter) (`_shared.py:11-24, 140-214`). Providers extend it: Anthropic honors server `Retry-After` and adds 529 (`agent/providers/anthropic.py:761-866`); OpenAI adds websocket codes and an auth/validation deny-list with retry-by-default for unknowns (`agent/providers/openai.py:1088-1149`). All SDK-internal retries are disabled (`max_retries=0`) so the wrapper is the single retry authority (`openai.py:120-125`, `anthropic.py:222-228`, `_shared.py:224-229`).

### Stateful adapters keep native history; the neutral format carries a lossless echo
The loop's message list is treated as an append-only event feed, not the truth. OpenAI keeps canonical Responses-API input items, diffs new messages in by index, and after the first response drops incoming assistant messages entirely — the model's own serialized `response.output` (including reasoning items) already lives in the store, and re-adding the lossy text copy would duplicate it (`openai.py:151-165, 788-866`). Anthropic stores the full serialized content-block list under `extra_content.anthropic.content` and replays those raw blocks instead of reconstructing from text+tool_calls, because extended thinking with tool use requires exact thinking blocks back (`anthropic.py:587-642, 379-396`). Gemini's codec is the sharpest case: `thought_signature` bytes on each part must return verbatim on later turns, so they are base64-encoded into JSON-safe history and decoded when rebuilding native `Part` objects (`agent/providers/gemini_codec.py:320-336, 383-473`); outbound conversion prefers deserializing `extra_content['google']['content']` over reconstruction (`gemini_codec.py:24-25`), tool-role messages are rewritten to user-role `FunctionResponse` parts (Gemini has no tool role, `gemini_codec.py:97-116`), and `Part(**kwargs)`/`FunctionCall(**kwargs)` are built inside `try/except TypeError` that drops unsupported kwargs to survive SDK version drift (`gemini_codec.py:51-55, 444-463`). OpenAI's websocket transport keeps three payloads (full / incremental with `previous_response_id` / fallback) and rotates connections at 3300s; `previous_response_not_found` downgrades to a full-context resend from local history instead of a dead run (`openai.py:603-786`).

### Compaction: one pure decision function, three execution mechanisms
`decide_compaction()` (`agent/providers/compaction_policy.py:22-223`) is side-effect-free and shared so pressure bands stay aligned across providers. Hard trigger: `prompt_tokens >= ceil(0.90 * danger_threshold)`, unconditional. Soft trigger fires only when the run is both *stuck* and *expensive*: a nonzero failure streak whose signature differs from the last one compacted, a pressure band match — `(0.85, streak≥3, items≥2) / (0.70, 4, 2) / (0.55, 5, 3)` — plus a +1 streak requirement when `cached_tokens/prompt_tokens ≥ 0.60` (cached prefixes are cheap; compaction destroys cache locality, `compaction_policy.py:162-165`), plus a 2-turn cooldown bypassed only on ≥1.20x token regrowth. Every negative outcome returns a named reason emitted as a `compaction_skipped` trace event. Execution always splits history `[immutable prefix (first ≤2 plain user messages) | compactable middle | raw tail since the last model response]` and differs per backend: OpenAI calls a server-side `responses.compact` endpoint with a cheaper model and re-counts tokens before/after (`openai.py:209-384, 506-589`); Gemini has no such endpoint, so a second LLM call with `temperature=0` and a strict JSON `response_schema` of five required string-arrays produces a summary rendered as a labeled user message headed `[System-generated compaction summary] / This is preserved prior run context. It is not a new user request.` (`gemini.py:571-631`, `gemini_codec.py:495-532`); the token-blind CLI provider compacts on rendered characters (160k, half at a ≥3 same-signature plateau) with an incremental rolling summary (`agent/providers/codex_cli.py:232-400`). Every failure mode of a compaction step degrades to a skipped-event; maintenance never kills the turn.

### Codecs: strict tool schemas, usage canonicalization
OpenAI strict mode demands all-required properties and `additionalProperties:false` at every level; the codec forces both and converts formerly-optional properties to nullable (`[T, 'null']` or an appended `{'type':'null'}` anyOf branch) so optionality survives — with a named per-tool escape hatch (`read_file` keeps its original required list, `strict:False`) for the one tool strict mode broke (`agent/providers/openai_codec.py:24-222`, `:199-219`). Gemini rejects `additionalProperties` and it is stripped (`gemini.py:722-725`). Usage is normalized once to one canonical key set (`prompt_tokens, candidates_tokens, total_tokens, cached_tokens, reasoning_tokens, cache_creation_input_tokens`), probing multiple SDK field spellings to survive version drift (`openai_codec.py:281-356`); Anthropic sums `input + cache_creation + cache_read` into `prompt_tokens` because its `input_tokens` excludes cached tokens (`anthropic.py:648-684`). Downstream cost/pressure/compaction code reads only canonical keys. Anthropic also merges consecutive tool-result messages into one user turn (required for parallel tool use, `anthropic.py:338-349`) and anchors prompt-cache breakpoints at exactly three places: system prompt, last tool schema, and the stable docs-prefix user message (`anthropic.py:476-552`).

### Cheap backends: one chat-completions mixin, ~100-line variants
`OpenAICompatibleChatCompletionsMixin` implements the whole contract once for any `/chat/completions` vendor (`agent/providers/chat_completions.py:26-214`). A variant like DashScope (`agent/providers/dashscope.py:78-170`) supplies only class attributes — `provider_name`, `base_url`, `supports_image_content`, `assistant_extra_content_key`, `assistant_extra_fields` — plus three template-method hooks: `_chat_extra_body` (vendor reasoning-knob spelling: DashScope `{enable_thinking, thinking_budget}`, DeepSeek `{thinking:{type}}`, OpenRouter `{reasoning:{enabled, effort}}`), `_chat_payload_extra_fields`, and `_response_extra_content` (extract vendor reasoning into `thought_summary` plus a namespaced `extra_content` dict that is later re-injected onto outgoing assistant messages, `chat_completions.py:244-252`). Every numeric limit wraps an env override around a code default, and `.env` loads with `override=False` so shell-exported credentials win (`dashscope.py:55-59`). `prepare_next_request` is a no-op — no token telemetry, no compaction. Since these vendors require `max_tokens`, it is clamped without a count API: estimate `(payload_chars+2)//3 + overheads`, then `min(configured, context - estimate - 1024)` with a floor of 16 so an over-full window yields a tiny valid response instead of a hard API error (`chat_completions.py:165-176`, `_shared.py:128-137`).

### The keyless outlier: a CLI subprocess as a provider
`codex_cli.py` shells out to a stateless CLI, re-rendering the entire conversation each call as one tagged prompt (`<system_prompt>`, `<available_tools>`, `<conversation>`), and forces the output shape with a JSON schema written to a temp file — `{content, thought_summary, tool_calls:[{name, arguments}]}` with `additionalProperties:false` and tool `name` constrained to an enum of the real tool names (`agent/providers/codex_cli.py:160-203, 925-975`). The response is strictly validated (exact key set, arguments must decode to a JSON object), usage is scraped from stdio lines, and 4000-char stdio tails go into `extra_content` for diagnostics. This proves the Protocol's worth: even a subprocess with no API fits behind the same five methods.

## Comparative: Claude Code's five-layer context ladder

Articraft compacts on two signals (context pressure, failure plateau). Claude
Code runs **five** independent mechanisms in a fixed order before every request
(`query.ts:365-467`), because they trade different things and compose:

| Order | Mechanism | What it drops | Why it goes here |
|---|---|---|---|
| 1 | **Per-message result budget** | Oversized tool results → disk, replaced by previews | Runs before microcompact, which keys purely on `tool_use_id` and never inspects content, so the two compose cleanly (`query.ts:369-394`) |
| 2 | **Snip** | Stale tool-result content, in place | Cheapest; frees tokens the later stages then do not have to summarise (`query.ts:400-410`) |
| 3 | **Microcompact** | Old results of a fixed set of tools — Read, shell, Grep, Glob, WebSearch, WebFetch, Edit, Write (`services/compact/microCompact.ts:41-50`) | Surgical: only tools whose output is re-derivable |
| 4 | **Context collapse** | Message ranges → summaries in a side store, projected at read time | Runs before autocompact so that if collapsing gets under the threshold, "we keep granular context instead of a single summary" (`query.ts:428-447`) |
| 5 | **Autocompact** | Everything → one summary | Last resort; loses structure |

The ordering principle is worth more than the list: **cheapest and most
reversible first, most destructive last, and each stage is allowed to make the
next unnecessary.**

**Reactive compaction is a separate axis.** When a request nonetheless comes
back too long, the error is withheld and a recovery ladder runs (drain
collapses → reactive compact → surface), each rung once
(`query.ts:1085-1183`). Media-size rejections join the same ladder but skip the
collapse rung, because collapse does not strip images (`query.ts:1074-1084`).
Proactive thresholds and reactive recovery are complementary; a system with
only one of them either compacts too eagerly or dies on the request that
crosses the line.

**Thresholds.** Autocompact fires at the effective context window minus a
13,000-token buffer; manual compaction reserves 3,000; three consecutive
failures trip a circuit breaker (`services/compact/autoCompact.ts:62-70`).
`MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000` (`autoCompact.ts:30`) is a
*reservation subtracted from the window*, not a cap on the summary call — the
primary fork path explicitly refuses to set `maxOutputTokens`, because doing so
would clamp the thinking budget and break cache-key parity with the main thread
(`services/compact/compact.ts:1181-1184`); only the streaming fallback applies
it. After compacting, up to **5 files at 5,000 tokens each** are re-read within
a shared 50,000-token attachment budget
(`services/compact/compact.ts:122-124`) — the 50,000 is the pool, not the file
allowance.

One more thing the constants hide: `WARNING_THRESHOLD_BUFFER_TOKENS` and
`ERROR_THRESHOLD_BUFFER_TOKENS` are both 20,000 (`autoCompact.ts:63-64`), so
the two-band warning design is currently one band. If you copy the structure,
either give the bands different values or admit there is one.

**Cache discipline is a first-class concern of this layer.** The system prompt
carries an explicit `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` marker separating
cross-organisation cacheable content from session-specific content
(`constants/prompts.ts:105-115`), and the tool array is sorted so built-ins form
a contiguous prefix ahead of externally discovered tools — a flat sort would let
a newly connected server interleave and invalidate every downstream cache key
(`tools.ts:354-366`). See reference 06 for the section-level machinery.

**Fallback across models mid-turn.** When the primary model is unavailable the
loop switches and *restarts the request*, discarding partial assistant messages
and rebuilding the tool executor (`query.ts:894-950`). A fallback that only
swaps the model id and replays history fails on the first thinking-enabled
turn, because thinking signatures are model-bound — "replaying a
protected-thinking block … to an unprotected fallback 400s"
(`query.ts:924-926`).

Note what the source does *not* do: the signature strip that fixes this is
gated on an internal-user flag (`query.ts:927`), so external builds hit the
failure the comment describes. Copy the reasoning, not the gate — this is a
place where the distilled source has a known gap rather than a solution.

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Contract is a `typing.Protocol` + normalized dict envelopes, not an ABC | Fakes satisfy it structurally; factory takes injectable constructors, so tests swap backends without monkeypatching | Nothing enforced at definition time; a method implemented by all providers but absent from the Protocol (`context_window_pressure`) can drift |
| Stateful providers keep canonical history in native API format; loop messages are an append-only feed diffed in by index | Reasoning items / thinking blocks / thought signatures are lossy or invalid through a neutral text format; verbatim replay preserves model quality and API validity | Dual sources of truth; needs defensive resync (Gemini resets when the message list shrinks, `gemini.py:374-377`) and makes providers per-conversation objects |
| Native fidelity travels through the neutral format via namespaced `extra_content` (`{'anthropic':…}`, `{'google':…}`) | Persistence stays provider-agnostic JSON while each codec round-trips its own extras; records from one provider degrade gracefully under another | Records store content twice (neutral + native); every codec needs both a native fast path and a reconstruct fallback |
| Compaction = shared pure decision function + per-provider execution | Thresholds behave identically everywhere and are unit-testable offline; execution genuinely differs (server endpoint vs LLM-summarize vs rolling char summary) | Telemetry-blind vendors get no compaction; each executor re-implements the prefix/middle/tail splice |
| A domain "stuck" signal (failure streak + signature) is plumbed into compaction policy | Fusing *stuck* with *expensive* makes a summary fire exactly when it pays for itself; signature dedup avoids re-summarizing the same plateau | Provider layer is not domain-agnostic; a new agent must map its own stuck signal or pass zeros |
| Never compact the prefix (first ≤2 user messages) or the raw tail (since last response) | Prefix holds task ground truth and anchors prompt caches; tail holds in-flight tool_call/result pairs that break if split | A huge current-turn tool result is uncompactable; the 0.90 hard valve is the only remaining relief |
| SDK retries disabled everywhere; one wrapper loop owns retry policy | No nested hidden backoff; uniform logging; honors server `Retry-After` | Wrapper re-implements status extraction per SDK via best-effort `getattr` probing |
| Unknown errors default to retry, bounded by attempts, behind an auth/validation deny-list | Aborting a long run wastes all prior spend; transiently weird errors are common | A novel permanent error burns attempts and backoff before surfacing; deny-list is brittle string matching |
| Key choice is random-per-run generally, per-request round-robin only for Gemini | One key per run keeps prompt caches and stored-response state coherent; Gemini's caches aren't key-bound and its limits are per-key | Random selection can collide two runs on one key; naive per-request rotation elsewhere would break caching |
| Compaction uses a cheaper dedicated model, never the generation model | Summarization is low-difficulty extraction; economics must not compete with generation spend | A weaker summarizer can drop the one detail that mattered; fixed-category schemas mitigate |
| Every maintenance action or skip emits a structured trace event with a machine-readable reason | Silent never-compacting or over-compacting runs are undebuggable otherwise | Boilerplate in every `prepare_next_request`; one-shot flags needed to avoid event spam |
| Provider variants over one mixin are class attributes + 3 hooks | Most OpenAI-compatible vendors differ only in base URL, reasoning-knob spelling, and which reply fields to echo; a new vendor is a one-file, near-zero-logic task | Structurally divergent APIs still need full implementations; the hook surface must stay stable or all variants churn |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| Transient HTTP statuses | `{408, 409, 425, 429, 500, 502, 503, 504}` (+529 for Anthropic) | Shared retryable set; other 4xx never retried (`_shared.py:11`, `anthropic.py:774`) |
| Retry defaults | attempts=4, base=0.5s, cap=20s, request timeout 900s — all env-overridable | ~40s of worst-case sleep per request cycle; 900s wall-clock guards hung SDK calls (`openai.py:95-98`) |
| Gemini retry override | 6 retries (7 attempts), 300s timeout | Free/preview tiers 429/503 more often; shorter timeout compensates for more attempts (`gemini.py:60-63`) |
| Hard-pressure trigger | 0.90 × danger-zone threshold | Compact before the relevance cliff at the window tail (`compaction_policy.py:43`) |
| Soft bands (pressure, streak, items) | (0.85, 3, 2) / (0.70, 4, 2) / (0.55, 5, 3) | Higher pressure justifies acting on shorter evidence (`compaction_policy.py:22-41`) |
| High-cache patience | cache ratio ≥ 0.60 → +1 required streak | Compaction destroys cache locality, so cached prompts clear a higher bar (`compaction_policy.py:44, 162-165`) |
| Soft cooldown | 2 turns, bypassed at ≥1.20× token regrowth | Prevents summarization becoming a reflex (`compaction_policy.py:45-46`) |
| Minimum turns before compaction | skip while turns ≤ 2 | Never compact a conversation that barely started (`openai.py:227-229`) |
| Immutable prefix | first ≤2 plain user messages | Task ground truth + cache anchor survive verbatim (`openai.py:581-589`) |
| Compaction models | `gpt-5.4-mini` / `gemini-3-flash-preview` | Cheap models so a summary pays for itself in a few turns (`openai.py:38`, `gemini.py:39`) |
| Websocket max connection age | 3300s | Rotate before the documented 60-min server cap kills a mid-request socket (`openai.py:102-105`) |
| Output safety margin / max_tokens floor | 1024 tokens / floor 16 | Clamp keeps requests valid at extreme pressure; tiny responses surface pressure instead of API errors (`chat_completions.py:165-176`) |
| Prompt-size estimate ratios | chat: `(chars+2)//3 + 128 + 16/msg + 128/tool`; Anthropic: `(chars+3)//4 + 256 + 24/msg + 128/tool` | Pessimistic 3-4 chars/token offline estimate; no count API call needed (`_shared.py:128-137`, `anthropic.py:894-903`) |
| CLI compaction threshold | 160,000 rendered chars (half at ≥3 same-signature failures); keep last 8 messages | Character proxy when no token accounting exists (`codex_cli.py:25-28`) |
| Anthropic cache TTL | ephemeral 5m default, opt-in 1h; on by default | Breakpoints at system prompt + last tool + docs prefix (`anthropic.py:476-493`) |
| Thinking-level canon | `{low, med, high, xhigh, max}`, `med→medium` at wire boundaries, default HIGH | One internal scale translated per provider (`articraft/values.py:16-21, 59-88`) |
| Compaction temperature | 0 (generation 0.7) | Determinism for state summarization (`gemini.py:481, 590`) |

## Reusable pattern

```python
"""Provider abstraction for a multi-backend tool-calling agent loop.
One loop, N backends: the loop speaks a neutral envelope; each adapter owns
its wire format, retries, caching, and context compaction. Stdlib only."""
from __future__ import annotations
import asyncio, json, logging, math, os, random
from typing import Any, Protocol

log = logging.getLogger("providers")

# ---- 1. The contract (structural typing: fakes need no inheritance) --------
class ProviderClient(Protocol):
    model_id: str

    def build_request_preview(self, *, system_prompt: str,
                              messages: list[dict], tools: list[dict]) -> dict:
        """Exact wire payload, zero network. Enables golden-file tests."""

    async def prepare_next_request(self, *, system_prompt: str,
                                   messages: list[dict], tools: list[dict],
                                   completed_turns: int, stuck_streak: int = 0,
                                   stuck_signature: str | None = None) -> dict:
        """Pre-turn maintenance: cache upkeep + compaction. Returns trace events."""

    async def generate_with_tools(self, system_prompt: str, messages: list[dict],
                                  tools: list[dict]) -> dict:
        """-> {content, tool_calls: [{id, type, function: {name, arguments}}],
              thought_summary?, usage?: {prompt_tokens, candidates_tokens,
              total_tokens, cached_tokens, reasoning_tokens},
              extra_content?: {<provider>: {...native blocks, bytes b64'd...}}}"""

    async def close(self) -> None: ...

# ---- 2. Key pools: FOO_API_KEY primary + FOO_API_KEYS pool -------------------
def keys_from_env(primary: str, pool: str) -> list[str]:
    raw = [os.environ.get(primary, "")]
    raw += os.environ.get(pool, "").replace("\n", ",").split(",")
    out: dict[str, None] = {}
    for k in (s.strip() for s in raw):
        if k:
            out.setdefault(k, None)
    return list(out)

def pick_key(primary: str, pool: str) -> str:
    keys = keys_from_env(primary, pool)
    if not keys:
        raise RuntimeError(f"set {primary} or {pool}")
    return random.choice(keys)  # per-RUN choice keeps server caches coherent;
                                # round-robin per request only when limits are
                                # per-key AND server state is not key-bound.

# ---- 3. Retry: one loop owns policy; disable SDK retries (max_retries=0) ----
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
DENY_FRAGMENTS = ("api key", "unauthorized", "invalid", "not found")

def should_retry(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int):
        return status in TRANSIENT_STATUS or status >= 500
    msg = str(exc).lower()
    return not any(f in msg for f in DENY_FRAGMENTS)  # unknown => retry, bounded

async def retry(op, *, context: str, attempts: int = 4, base: float = 0.5,
                cap: float = 20.0, server_hint=lambda e: None) -> Any:
    for i in range(1, attempts + 1):
        try:
            return await op()
        except Exception as exc:
            if i >= attempts or not should_retry(exc):
                raise
            delay = server_hint(exc) or random.uniform(
                0, min(cap, base * 2 ** (i - 1)))          # FULL jitter
            log.warning("%s failed (attempt %d/%d), retrying in %.1fs: %r",
                        context, i, attempts, delay, exc)
            await asyncio.sleep(delay)

# ---- 4. Compaction decision: pure, shared, unit-testable --------------------
BANDS = ((0.85, 3, 2), (0.70, 4, 2), (0.55, 5, 3))  # (pressure, streak, items)

def decide_compaction(*, prompt_tokens: int, cached_tokens: int,
                      danger_tokens: int, stuck_streak: int,
                      stuck_signature: str | None,
                      last_compacted_signature: str | None,
                      compactable_items: int, turn: int,
                      last_soft_turn: int, last_soft_tokens: int) -> str | None:
    if prompt_tokens >= math.ceil(0.90 * danger_tokens):
        return "hard"
    if not stuck_streak or stuck_signature == last_compacted_signature:
        return None                # dedupe: never re-summarize the same plateau
    pressure = prompt_tokens / danger_tokens
    band = next((b for b in BANDS if pressure >= b[0]), None)
    if band is None:
        return None
    need = band[1] + (1 if cached_tokens >= 0.60 * prompt_tokens else 0)
    if stuck_streak < need or compactable_items < band[2]:
        return None                # cached prefixes are cheap: be patient
    if turn - last_soft_turn < 2 and prompt_tokens < 1.20 * last_soft_tokens:
        return None                # cooldown unless context regrew fast
    return "soft"

# Execution (per provider): split [prefix(first <=2 user msgs) | middle | tail
# since last model response]; summarize ONLY the middle with a CHEAPER model;
# splice back; clear server-side state ids; emit event or skipped(reason=...).
COMPACTION_HEADER = ("[System-generated compaction summary]\n"
                     "This is preserved prior run context. "
                     "It is not a new user request.")

# ---- 5. Cheap backends: one mixin; variants = class attrs + 3 hooks ---------
class ChatCompletionsMixin:
    provider_name = "override-me"
    base_url = "override-me"
    extra_content_key = "override-me"        # namespace for native reply fields
    extra_roundtrip_fields: tuple[str, ...] = ()   # echoed back on later turns

    def _extra_body(self) -> dict:           # vendor reasoning-knob spelling
        return {}

    def _extract_extra(self, message: dict) -> tuple[str | None, dict]:
        raw = {f: message.get(f) for f in self.extra_roundtrip_fields
               if message.get(f)}
        summary = next(iter(raw.values()), None)
        return summary, {self.extra_content_key: raw}

    def clamp_max_tokens(self, payload: dict, *, context_tokens: int,
                         configured_max: int, safety: int = 1024) -> int:
        chars = len(json.dumps(payload))
        estimate = (chars + 2) // 3 + 128    # ~3 chars/token, pessimistic
        return max(min(configured_max, context_tokens - estimate - safety), 16)

    async def prepare_next_request(self, **_: Any) -> dict:
        return {}                            # no token telemetry => no compaction
```

## Preflight: the model ID is not something you remember

Companion: `templates/agentkit/preflight.py`.

A model that writes an agent picks the agent's model ID out of its own
training data. That data is always behind the provider's catalogue, and it
says nothing about which models *this account* is entitled to — so every
generated agent carries a guess wearing the clothes of a fact. The failure is
not exotic. A generator asked for `gpt-5.6`; no such model existed, while the
account had `gpt-5.6-luna`, `gpt-5.6-sol` and `gpt-5.6-terra`.

    Never write a model ID from memory. Ask the provider, then match exactly.

`resolve_model()` has four outcomes and three are refusals:

| Outcome | Meaning |
|---|---|
| `EXACT` | the ID is in the catalogue verbatim — the only usable result |
| `AMBIGUOUS` | the ID is the family of one or more real models — refused |
| `MISSING` | nothing matches; nearest names offered as a hint, never a substitution |
| `UNVERIFIED` | no catalogue was reachable — reported, never treated as a pass |

**Never auto-pick a variant.** Siblings differ in price, latency, context and
capability. Resolving `gpt-5.6` to the first match is how an agent ends up
running on a model nobody chose, with the invoice as the notification. Even a
*single* match is refused: a near-match is not a match.

`parse_model_list()` accepts the three catalogue shapes — OpenAI-compatible and
Anthropic's `{"data": [{"id": ...}]}`, and Google's `{"models": [{"name":
"models/..."}]}` — and yields `[]` for anything unrecognised, because a
misparse would report a present model as MISSING and send someone hunting.

`preflight()` folds in the rest of what must hold before turn one: the key's
env var is set (presence only — the value is never read into a message), the
resolved model meets the declared `Requirement` for context, output and
modality, and a one-token call actually succeeds. Listing and pinging are
network, so they sit behind `ModelCatalogue`; `UnconfiguredCatalog` refuses and
`curl_for()` prints the exact request to run by hand.

## Pitfalls

- Dual source of truth corrupts silently: a provider keeping native canonical history must **drop incoming assistant messages after the first response** (`openai.py:788-807`, `gemini.py:385-386`) and reset its store when the loop's message list shrinks (`gemini.py:374-377`) — miss either and every turn duplicates model output.
- Opaque reasoning artifacts must round-trip byte-exact: base64-encode thought-signature bytes into JSON history and decode on replay (`gemini_codec.py:320-336`); replay Anthropic thinking blocks verbatim from `extra_content` rather than reconstructing (`anthropic.py:379-386`). Rebuilding from text breaks tool calling.
- Server-side conversation state is a lease, not a guarantee: handle "previous response not found" with a full-context resend from local history (`openai.py:637-665`), and exclude that error from blind retry (`openai.py:1101-1103`) — a retry can never fix it.
- Strict tool schemas break individual tools; plan per-tool escape hatches, not a uniform pass (`openai_codec.py:199-219`). Also convert formerly-optional params to nullable under all-required rewrites (`openai_codec.py:24-84`), or the model is forced to invent values.
- Capability differences surface as errors, not feature flags: sniff unsupported-parameter 400/422 text and retry without the parameter (`openai.py:420-439, 1050-1085`).
- Maintenance must never kill the turn: token-count failures become `estimate_error` on the event (`openai.py:317-347`); every CLI compaction failure degrades to a skipped trace event (`codex_cli.py:294-334`).
- Naive "compact when big" wastes money on well-cached prompts — raise the bar when cache ratio is high (`compaction_policy.py:162-165`).
- A compaction summary injected as a user message gets re-interpreted as a fresh instruction unless explicitly labeled otherwise (`gemini_codec.py:521-524`).
- Merge adjacent tool results into one user turn for APIs that require it (`anthropic.py:338-349`); one message per result breaks parallel tool use.
- Usage semantics differ per vendor — normalize once: Anthropic `input_tokens` excludes cached/created tokens (sum three fields, `anthropic.py:659-661`); cached tokens hide under several SDK spellings (`openai_codec.py:289-330`). Cost math against raw fields double- or under-counts.
- Copy-paste bugs in shared helpers hide for months: DeepSeek passes its singular key name as the pool name, so plural rotation silently does not exist (`deepseek.py:45`). Test both env names per provider.
- Magic-string couplings between prompt content and provider code (the docs-heading cache anchor, `anthropic.py:513-527`) break silently when the prompt is reworded — document them next to both sides.
- Defensive SDK construction is mandatory with unpinned deps: build native types in `try/except TypeError` dropping unknown kwargs (`gemini_codec.py:51-55, 459-463`), and parse tool arguments with a `{'_raw': args}` fallback (`gemini_codec.py:77-81`).
- Subprocess transports need hard output validation: exact key sets, JSON-object arguments, enum-constrained tool names in the schema itself (`codex_cli.py:680-770, 951-962`).
- Env-override helpers that swallow parse errors turn typo'd overrides into silently-ignored config; log or fail on unparseable values.

## Checklist

- [ ] Define the provider contract as a structural Protocol with a normalized response envelope (`content`, `tool_calls`, `thought_summary`, canonical `usage`, namespaced `extra_content`)
- [ ] Give every constructor a `dry_run` mode and a `build_request_preview` so the exact wire payload is golden-testable offline
- [ ] Centralize enums (provider names, thinking levels) in one module; add model-id prefix inference so users can omit the provider flag
- [ ] Validate credentials and binaries before the first turn; refuse silent model-id defaults where inheritance is dangerous
- [ ] Support key pools (singular + plural env vars); pick per-run by default, per-request round-robin only where server state is not key-bound
- [ ] Disable all SDK-internal retries; own one retry loop with full jitter, transient-status classification, an auth deny-list, and server `Retry-After` support
- [ ] Wrap every network call in a hard wall-clock timeout
- [ ] Keep native-format history as the canonical store in stateful adapters; diff loop messages in by index and drop post-first-response assistant messages
- [ ] Round-trip provider-native artifacts through namespaced `extra_content`, base64-encoding any bytes for JSON persistence
- [ ] Normalize usage to one canonical key set at the codec boundary; downstream code never touches vendor shapes
- [ ] Write the compaction *decision* as a pure shared function (hard pressure valve + stuck-and-expensive soft trigger + cache patience + cooldown); implement *execution* per provider over an immutable prefix / middle / raw tail split
- [ ] Use a cheaper dedicated model for summarization, and label injected summaries as preserved context, not a new request
- [ ] Emit a structured trace event with a machine-readable reason for every maintenance action taken or skipped
- [ ] Add OpenAI-compatible vendors as class-attribute subclasses of one chat-completions mixin (base URL + reasoning-knob hook + extra-content extraction hook)
- [ ] Clamp `max_tokens` from a pessimistic character-based prompt estimate with a safety margin and a small floor
- [ ] Map your domain's "stuck" signal (failed builds, failing tests, rejected plans) onto the compaction policy inputs — or pass zeros to disable soft compaction
