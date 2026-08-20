"""Minimal provider seam: one normalized completion call per turn.

Distills ``articraft.agent.provider`` (the anthropic/gemini/openai/openrouter
adapters and their shared key/retry plumbing) plus the codec discipline used
by ``articraft.agent.compaction`` and the dry-run payload preview.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

Message = dict[str, Any]


@dataclass
class Completion:
    """The only shape the loop ever sees, whatever the wire format was."""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #   each call: {"id": str, "name": str, "arguments": dict | json-str}
    usage: dict[str, int] = field(default_factory=dict)
    #   normalized keys: prompt, cached, cache_write, output, total
    extra: dict[str, Any] = field(default_factory=dict)
    #   lossless echo of native blocks (thinking, signatures), by provider key


class Codec(Protocol):
    """Wire-format seam: harness shapes in, Completion out.  One per format."""

    def encode_request(self, system: str, messages: list[Message],
                       tools: list[dict[str, Any]]) -> dict[str, Any]: ...
    def decode_response(self, raw: dict[str, Any]) -> Completion: ...


# --- key rotation ------------------------------------------------------------
def keys_from_env(primary: str, pool: str | None = None) -> list[str]:
    """FOO_API_KEY plus optional FOO_API_KEYS (comma/newline separated), deduped."""
    keys: list[str] = []
    single = os.environ.get(primary, "").strip()
    if single:
        keys.append(single)
    for part in os.environ.get(pool or "", "").replace("\n", ",").split(","):
        if part.strip() and part.strip() not in keys:
            keys.append(part.strip())
    return keys


def pick_key(keys: list[str]) -> str:
    """Per-run random pick; use per-request round-robin when limits are per-key."""
    if not keys:
        raise RuntimeError("no API key configured")
    return random.choice(keys)


# --- thinking-level mapping: one generic knob -> each backend's spelling -----
THINKING_LEVELS: dict[str, dict[str, Any]] = {
    "anthropic": {"off": None, "low": 2048, "medium": 8192, "high": 16384},
    "openai": {"off": "none", "low": "low", "medium": "medium", "high": "high"},
    "gemini": {"off": 0, "low": 1024, "medium": 8192, "high": 24576},
}


# --- retry: full jitter, transient-only; disable the SDK's own retries -------
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def with_retries(op: Callable[[], Any], *, attempts: int = 4, base: float = 0.5,
                 cap: float = 20.0,
                 should_retry: Callable[[Exception], bool] | None = None) -> Any:
    for attempt in range(1, attempts + 1):
        try:
            return op()
        except Exception as exc:
            retryable = should_retry(exc) if should_retry else True
            if attempt >= attempts or not retryable:
                raise
            time.sleep(random.random() * min(cap, base * 2 ** (attempt - 1)))
    raise RuntimeError("unreachable")


class ChatAdapter:
    """Backend skeleton: subclass = class attrs + three hooks + a transport.

    A whole family of cheap OpenAI-compatible backends can share this class,
    each subclass only overriding the attrs and hooks (the articraft pattern).
    """

    provider_name = "generic"
    base_url = ""                       # e.g. "https://api.example.ai/v1"
    extra_content_key = "generic"       # namespace for lossless native echoes

    def __init__(self, model_id: str, codec: Codec, *,
                 thinking_level: str = "high", dry_run: bool = False) -> None:
        self.model_id = model_id
        self.codec = codec
        self.thinking_level = thinking_level
        self.dry_run = dry_run          # dry-run instances build payloads only
        self.system_prompt = ""
        env = self.provider_name.upper()
        self._keys = [] if dry_run else keys_from_env(f"{env}_API_KEY",
                                                      f"{env}_API_KEYS")

    # --- hooks subclasses usually override ----------------------------------
    def _extra_body(self) -> dict[str, Any]:
        """Provider-specific reasoning knob, resolved via THINKING_LEVELS."""
        level = THINKING_LEVELS.get(self.provider_name, {}).get(self.thinking_level)
        return {} if level in (None, "none", 0) else {"thinking": level}

    def _extract_native(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Native fields to round-trip verbatim (reasoning_content, signatures)."""
        return {}

    def _should_retry(self, exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        return status is None or status in TRANSIENT_STATUS   # unknown => retry

    # --- transport: the ONLY code that touches the network ------------------
    def _post(self, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        raise NotImplementedError("wire in your HTTP client or SDK here; "
                                  "wrap the call in a hard timeout")

    # --- the contract the loop calls ----------------------------------------
    def build_request_preview(self, messages: list[Message],
                              tools: list[dict[str, Any]]) -> dict[str, Any]:
        """The EXACT wire payload, no network — powers dry-run and golden tests."""
        payload = self.codec.encode_request(self.system_prompt, messages, tools)
        payload["model"] = self.model_id
        payload.update(self._extra_body())
        return payload

    def complete(self, messages: list[Message],
                 tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the dict shape agent_loop.py expects."""
        payload = self.build_request_preview(messages, tools)
        raw = with_retries(lambda: self._post(payload, pick_key(self._keys)),
                           should_retry=self._should_retry)
        completion = self.codec.decode_response(raw)
        native = self._extract_native(raw)
        if native:                       # lossless echo for the next request
            completion.extra.setdefault(self.extra_content_key, native)
        return {"text": completion.text, "tool_calls": completion.tool_calls,
                "usage": completion.usage, "extra": completion.extra}
