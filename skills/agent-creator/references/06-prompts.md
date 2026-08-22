# 06. System Prompt Architecture

**Maps to:** SystemPrompt · Guardrails · State/Context · Tools · Memory · Cost · **Distilled from:** Articraft `agent/prompts/{spec,compile,loader}.py`, `agent/tools/__init__.py`, `agent/workspace_docs.py`, `agent/harness_guidance.py`, `agent/harness.py`, `storage/trajectories.py` · Claude Code 2.1.88 `src/constants/systemPromptSections.ts`, `src/constants/prompts.ts`, `src/utils/attachments.ts`

## Why this module exists

An agent that must run against multiple LLM providers cannot ship one system prompt: providers differ in tool contracts (freeform patch vs. JSON replace/write), verbosity habits, and failure modes, while the agent's identity, quality bar, and domain rules must stay identical everywhere. Hand-maintaining N full prompts guarantees drift; assembling strings at runtime destroys diffability, cacheability, and reproducibility. This module treats prompts as code: shared markdown sections deterministically compiled into committed per-provider artifacts, resolved by provider at load time, content-addressed for run records, kept deliberately small by pushing documentation into the first user message and everything else behind a read tool, and supplemented mid-loop by targeted, deduplicated corrective messages instead of a system prompt that tries to pre-empt every failure.

## How Articraft implements it

### Declarative variant spec: shared sections, per-provider outputs

`PromptVariant(name, sections, output, description)` is a frozen dataclass; `PROMPT_VARIANTS` lists 6 variants (openai, codex_cli, gemini, openrouter, anthropic, deepseek) (`agent/prompts/spec.py:11-93`). Every variant composes the same ordered skeleton: `designer_common.md` (identity + quality bar) → `link_naming.md` (deliverable-naming conventions) → one or more `provider_*.md` (tool contract + process) → `sdk_base.md` (library modeling/testing rules). Sections are shared: anthropic and openrouter reuse the same list and compile byte-identically; deepseek reuses openrouter's process section with its own tools section (`agent/prompts/spec.py:19-89`). Fix a rule once, recompile all variants.

### Deterministic compiler with a staleness gate

`compile_prompt_variant` strips trailing newlines per section, joins non-empty parts with `"\n\n"`, and guarantees exactly one trailing newline — fully deterministic, so artifacts diff cleanly (`agent/prompts/compile.py:10-20`). `write_compiled_prompt` is idempotent (rewrite only on change, `compile.py:23-32`). `find_stale_prompts` recompiles in memory and diffs against `generated/` (`compile.py:44-54`); the CLI supports `--check` exiting 1 on staleness (`compile.py:57-84`), and the test suite imports `find_stale_prompts` so editing sections without recompiling fails CI (`tests/agent/test_prompting_compile.py`). Generated files are committed, never built at runtime.

### Provider-aware resolution behind one generic name

Callers pass a single default, `system_prompt_path='designer_system_prompt.txt'` — a file that never exists on disk (`agent/harness.py:194`). `resolve_system_prompt_path` maps single-name relative paths into the generated dir, asks the SDK profile to translate the provider enum into a concrete filename (openai→`_openai.txt`, openrouter AND dashscope→`_openrouter.txt`, etc., `sdk/_profiles.py:38-58`), and tries the provider-specific candidate FIRST when the requested name is a known default, so explicit custom paths still win (`agent/prompts/loader.py:27-66`). `load_system_prompt_text` returns `(resolved_path, text)` and raises `FileNotFoundError` if nothing exists (`loader.py:69-84`); the harness calls it once in `__init__` (`agent/harness.py:278-281`, via the `_build_system_prompt` wrapper at `harness.py:614-619`).

### What the compiled prompt actually contains

The compiled artifact is ~10–15 KB with four XML-tagged blocks (`agent/prompts/generated/designer_system_prompt_openai.txt:1-64`, whose sha256 is `7774bda5...` — the name it gets when content-addressed into the runtime store `data/system_prompts/`): `<role>` — identity, sandbox contract (one writable file, read-only docs tree), success definition, an instruction to derive a compact internal brief before the first edit, 4 numbered hard requirements, anti-gaming rules ("Use compile output, QC, and tests as sensors — not optimization targets"), and full-autonomy rules ("Do not ask the user for feedback… Finish the task autonomously"); `<link_naming>` — output-naming conventions; `<tools>` — the provider's tool list, edit-tool quirks (e.g. `apply_patch` is FREEFORM raw text, not JSON), read-before-patch, small patches; `<modeling>` — entry-point contract, import discipline, test-scope rules. Chatty providers additionally carry explicit termination discipline ("After a clean compile… conclude immediately"; no post-success refinement "without a named defect") in their tools sections — the plain OpenAI section does not.

### Docs live in the first user message, not the system prompt

`build_first_turn_messages` emits TWO user messages: the preloaded docs bundle, then the task prompt with a `<runtime_task_guidance>` block prepended (`agent/tools/__init__.py:66-179`). Only 3 index/reference docs are preloaded (`_DEFAULT_PRELOAD_PATHS`, `agent/workspace_docs.py:11-15`); ~23 more sit behind a `read_file` tool through a whitelisted virtual path namespace (`_DOC_PATH_ALIASES`, `workspace_docs.py:171-198`) with traversal rejection (`workspace_docs.py:66-77`). The code comment states the intent: "Detailed docs now live behind read_file(path=...), so the preloaded bundle stays small" (`workspace_docs.py:97`). First-turn construction runs only when the conversation is empty; resumed runs append the raw user message (`agent/harness.py:1162-1175`).

### Mid-loop guidance injector: deterministic detectors + sha dedup

`GuidanceInjector` appends synthetic user-role, XML-tagged messages after tool execution, in a fixed order (edit-retry → api-error → code-contract, `agent/harness.py:1475-1489`; `agent/harness_guidance.py:121-374`). Detectors are cheap and deterministic: exact string match on tool errors ("Could not find the old_string" → `<edit_retry_guidance>`, `harness_guidance.py:321-374`); keyword match on compile output (`API_ERROR_KEYWORDS`, `harness_guidance.py:25-35`) → probe-with-`inspect.signature` guidance, deduped by `sha256(matched_keywords + output[:800])` (`harness_guidance.py:243-304`); AST scan of the current working file — only after a successful mutating tool call — detecting tests that reference now-missing names or re-implement compiler-owned QC (`harness_guidance.py:64-118, 306-319`). Every guidance family keeps a signature set so identical advice never repeats.

### No-action escalation ladder

Empty responses (no text, no tool calls) increment a streak counter (`agent/harness.py:1383-1386`). Streak 1: mild state-aware reminder — conclude if the code compiled clean, else compile (`harness.py:429-438`). Streak ≥ 2: strong escalation naming concrete tools and embedding provider diagnostics (`harness.py:440-475`). Streak ≥ 3 with escalation already sent: abort the run with an error terminate reason to stop burning turns and cost (`harness.py:1387-1420`; constants at `harness.py:62-63`).

### Content-addressed persistence + cache keying

`ensure_shared_system_prompt_text` stores each run's prompt at `data/system_prompts/<sha256(text)>.txt`, verifying content equality on hash hit (`storage/trajectories.py:52-66`); run records store only the sha. The same hashing keys OpenAI prompt caching: `prompt_cache_key = 'ac1:' + digest of {provider, model_id, sdk_package, sha256(system_prompt), sha256(docs), sha256(tool_schemas)}`, capped at 64 chars (`agent/harness.py:138-183`) — server cache reuse exactly when the full static prefix is unchanged. Non-system prompts live in the same `sections/` dir: `gemini_compaction.md` is loaded at runtime as the history-compaction instruction (`agent/prompts/loader.py:87-91`, `agent/providers/gemini.py:811-813`), keeping ALL prompt text in one versioned directory.

## Comparative: Claude Code's prompt assembly

Articraft compiles per-provider prompt artifacts at build time. Claude Code
assembles at *run* time, and its machinery answers a question Articraft's does
not face: how to keep a prompt that changes every session cacheable.

**Sections are memoised, and volatility is opt-in with a reason.**
`systemPromptSection(name, compute)` caches until `/clear` or `/compact`;
`DANGEROUS_uncachedSystemPromptSection(name, compute, reason)` recomputes every
turn and **requires a reason argument** explaining why cache-breaking is
necessary (`constants/systemPromptSections.ts:16-38`). The API makes the
expensive choice loud at the call site — the single cheapest technique in this
whole reference.

**One marker separates the cacheable prefix from the volatile tail.**
`SYSTEM_PROMPT_DYNAMIC_BOUNDARY` splits content that can be cached across
*organisations* from user- and session-specific content, with a warning naming
the two files that must be updated together if it moves
(`constants/prompts.ts:105-115`).

**Volatile lists live in messages, never in the prompt or a tool description.**
The subagent list was interpolated into a tool description; MCP servers
connecting asynchronously mutated it; each mutation invalidated the whole
tool-schema cache — **~10.2% of fleet cache-creation tokens**
(`tools/AgentTool/prompt.ts:48-59`). Same principle at a smaller scale: the
per-UID temp directory is rewritten to the literal `$TMPDIR` so the shell tool's
description is byte-identical across users and can share a cross-user cache
(`tools/BashTool/prompt.ts:185-190`).

**Runtime guidance is a closed vocabulary of typed attachments.** Articraft's
guidance injection generalises here into ~40 named attachment kinds appended as
user-role messages between turns — `todo_reminder`, `nested_memory`,
`relevant_memories`, `skill_listing`, `skill_discovery`, `plan_mode`,
`diagnostics`, `queued_command`, `hook_additional_context`, `edited_text_file`,
`max_turns_reached`, `critical_system_reminder`, and more
(`utils/attachments.ts:440-621`). Typing them is what makes them auditable,
suppressible, and strippable before compaction — several are re-injected after a
compact anyway, so they are removed from the summary input rather than
summarised (`services/compact/compact.ts:203-224`).

**Reminders are rate-limited, and the budget is cumulative.** The todo reminder
fires only after 10 turns without a write and at most every 10 turns
(`utils/attachments.ts:254-257`); plan-mode and auto-mode attachments every 5
turns with a full restatement every 5th (`utils/attachments.ts:259-267`). Memory
injection is capped three ways — 200 lines, 4 KB per file, and a cumulative
**60 KB per session**, after which prefetching stops entirely — with the
reasoning recorded inline: a per-turn cap bounds one injection, but "over a long
session the selector keeps surfacing distinct files — ~26K tokens/session
observed in prod" (`utils/attachments.ts:269-289`). Any recurring injection needs a
session-level budget, not just a per-turn one.

**Tool descriptions are prompts too.** The shell tool's description is built at
runtime from feature flags, sandbox configuration, git settings and timeouts,
and every reference to a sibling tool is an imported name constant rather than a
string literal (`tools/BashTool/prompt.ts:275-369`) — so renaming a tool cannot
leave a stale instruction behind. It also actively steers the model *away from
itself*: "use Read not cat, Edit not sed", on the grounds that a general tool
used for a specific job is harder for the user to review and permission
(`tools/BashTool/prompt.ts:280-291`).

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Compile shared markdown sections into committed per-provider artifacts, not runtime string assembly | Providers need different tool contracts; identity/quality/domain rules must stay identical. Committed artifacts are diffable, hashable, and staleness-testable | Two sources of truth (sections + generated) need a CI gate; no runtime templating — dynamic content must arrive as messages |
| Resolve one generic prompt name to a provider variant at load time; explicit paths override | Callers pass a single config value; adding a provider = section file + spec entry + profile mapping, no call-site changes | The configured filename never exists on disk; debugging requires knowing candidate-list order |
| Docs in first user message; preload only 3 index docs, rest behind `read_file` | System prompt stays small and stable (its sha feeds the cache key); quickstart acts as router so the model pulls docs on demand | Model must actually follow the index — countered by "do not guess APIs" rules plus the API-error injector |
| Mid-loop corrections as synthetic user messages with XML tags and sha dedup, from deterministic detectors | Just-in-time guidance beats a prompt covering every failure mode; user-role works on all provider APIs; dedup prevents context bloat | Detectors couple to exact error strings; AST scan no-ops on broken syntax; model can't tell guidance from real user input |
| Anti-reward-hacking language baked into the role section, enforced mechanically by AST injectors | Self-correcting agents optimize the sensor (delete geometry to pass checks, blanket overlap allowances) unless told sensors are not targets | Longer prompt; prompt-level enforcement stays advisory, only partially mechanized |
| Termination discipline written into chatty providers' tools sections only | Gemini/DeepSeek/Codex burn turns on post-success reflection; "stop unless you can name a defect" is an explicit rule | Risks premature conclusion when a defect exists but is unnamed |
| Content-address every run's prompt by sha256; store one shared copy | Dedup across thousands of runs; any drift produces a new hash; collision mismatch raises | Hash-named files need tooling to map hash → variant |
| Escalation ladder: gentle → strong → abort at 3 empty turns | Thinking models emit reasoning-only turns; two increasingly explicit chances before cutting losses | Global constants, not per-provider; legitimate long thinking gets nudged with extra tokens |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| `PROMPT_VARIANTS` | 6 (anthropic == openrouter byte-identical) | One artifact per distinct (tool contract × process) combination, not per provider name |
| Section order | identity → conventions → provider tools → domain rules, joined `"\n\n"` | Stable ordering keeps diffs minimal; identity first, mechanics middle, domain rules last |
| `_DEFAULT_PRELOAD_PATHS` | 3 docs (index + 2 references) | Small first-turn payload; ~23 other docs stay behind `read_file` |
| `_DOC_PATH_ALIASES` | 26 whitelist mappings | Virtual namespace decoupled from repo layout; whitelist doubles as access control |
| `MUTATING_TOOL_NAMES` | `{apply_patch, replace, write_file}` | Contract AST scan runs only after a successful mutation — no redundant scans |
| `API_ERROR_KEYWORDS` | 7 lowercase substrings | Cheap classifier for "model guessed a nonexistent API" failures |
| API-error dedup window | `sha256(keywords + output[:800])` | Same failure → coach once; genuinely new failure text → fresh guidance |
| `NO_ACTION_ESCALATION_STREAK` / `MAX_CONSECUTIVE_NO_ACTION_TURNS` | 2 / 3 | Gentle at 1, hard at 2, abort at 3 consecutive empty turns |
| System prompt sizes | 10–15 KB per variant | Bulk knowledge lives in on-demand docs; small prefix keeps caching cheap |
| Prompt cache key | `'ac1:' + 43-char urlsafe-base64 sha256`, ≤ 64 chars | Reuse iff the entire static prefix (prompt + docs + tool schemas) is identical |
| Default prompt name | `designer_system_prompt.txt` (never exists) | One config value for every provider via resolver indirection |

## Reusable pattern

```python
"""Prompt-as-code: compiled variants, provider resolution, first-turn docs,
mid-loop guidance injection, empty-turn escalation, content-addressed persistence."""
import ast, hashlib
from dataclasses import dataclass
from pathlib import Path

SECTIONS, GENERATED, PROMPT_STORE = Path("sections"), Path("generated"), Path("store")

# ---- 1. Declarative build matrix: sections may be SHARED across variants ----
@dataclass(frozen=True)
class Variant:
    name: str; sections: tuple[Path, ...]; output: Path

def make_variants(providers: dict[str, list[str]]) -> list[Variant]:
    return [Variant(p, tuple(SECTIONS / s for s in
                    ["common_identity.md", "output_conventions.md",   # identity, quality bar,
                     *tool_sections, "domain_rules.md"]),             # anti-gaming, autonomy
                    GENERATED / f"system_prompt_{p}.txt")
            for p, tool_sections in providers.items()]

def compile_variant(v: Variant) -> str:  # deterministic => diffable + stable sha
    parts = [s.read_text().rstrip("\n") for s in v.sections]
    return "\n\n".join(p for p in parts if p) + "\n"

def find_stale(variants) -> list[Variant]:  # wire into CI: fail on section drift
    return [v for v in variants
            if not v.output.exists() or v.output.read_text() != compile_variant(v)]

# ---- 2. Provider-aware resolution: callers pass ONE generic name ----
KNOWN_DEFAULTS = {"system_prompt.txt"}
def resolve_prompt(path: str, provider: str, name_for: dict[str, str]) -> Path:
    candidates = []
    if Path(path).name in KNOWN_DEFAULTS:                 # variant tried first,
        candidates.append(GENERATED / name_for[provider]) # explicit path still wins
    candidates.append(Path(path))
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(candidates)

# ---- 3. First turn: docs preload + runtime guidance live in USER messages ----
GUIDANCE = "<runtime_task_guidance>\nRead the work file first. Verify. Conclude when clean.\n</runtime_task_guidance>"
def build_first_turn(task: str, preload_docs: dict[str, str]) -> list[dict]:
    docs = "# Workspace Documentation (read-only)\n\n" + "\n\n".join(
        f"## {p}\n```markdown\n{t}\n```" for p, t in preload_docs.items())
    return [{"role": "user", "content": docs},            # FEW index docs only; the
            {"role": "user", "content": f"{GUIDANCE}\n\n{task}"}]  # rest via a read tool

# ---- 4. Mid-loop guidance: deterministic detectors + sha dedup ----
API_ERROR_KEYWORDS = ("attributeerror", "unexpected keyword argument")
MUTATING_TOOLS = {"apply_patch", "replace", "write_file"}

class GuidanceInjector:
    def __init__(self): self._seen: set[str] = set()
    def _once(self, key: str) -> bool:
        sig = hashlib.sha256(key.encode()).hexdigest()
        return sig not in self._seen and not self._seen.add(sig)
    def _say(self, convo, tag, text):  # synthetic user turn, XML-tagged
        convo.append({"role": "user", "content": f"<{tag}>\n{text}\n</{tag}>"})

    def after_tools(self, convo, calls, results, workfile: Path):
        if any("old text not found" in (r.error or "") for r in results):
            if self._once("edit_retry"):
                self._say(convo, "edit_retry_guidance",
                          "Re-read the file; retry with a smaller exact snippet.")
        for r in results:                                  # domain-API-guess coaching
            kws = sorted(k for k in API_ERROR_KEYWORDS if k in (r.output or "").lower())
            if kws and self._once(str(kws) + (r.output or "")[:800]):
                self._say(convo, "api_error_guidance",
                          "Do not guess APIs. Probe signatures, then make one small fix.")
        if any(r.ok and c.name in MUTATING_TOOLS for c, r in zip(calls, results)):
            try:
                tree = ast.parse(workfile.read_text())     # SyntaxError => skip; the
            except SyntaxError:                            # compile loop owns that case
                return
            missing = referenced_test_names(tree) - defined_names(tree)
            if missing and self._once(str(sorted(missing))):
                self._say(convo, "contract_guidance",
                          f"Tests reference missing names: {sorted(missing)}. "
                          "Restore them or fix the tests in the same edit.")

# ---- 5. Empty-response escalation ladder (in the main loop) ----
ESCALATE_AT, ABORT_AT = 2, 3
def handle_no_action(convo, streak: int, escalated: bool, diagnostics: str):
    if streak >= ABORT_AT and escalated:
        raise RuntimeError(f"aborted: {ABORT_AT} consecutive empty turns. {diagnostics}")
    if streak >= ESCALATE_AT:
        convo.append({"role": "user", "content":
            f"<action_required>\nCall a tool (apply_patch/replace/write_file) or return "
            f"a final visible response now. Diagnostics: {diagnostics}\n</action_required>"})
        return True                                        # escalation sent
    convo.append({"role": "user", "content":
        "<final_response_required>\nReturn a visible final response, or call a tool.\n"
        "</final_response_required>"})
    return escalated

# ---- 6. Reproducibility: content-address the prompt each run actually used ----
def persist_prompt(text: str) -> str:
    sha = hashlib.sha256(text.encode()).hexdigest()
    p = PROMPT_STORE / f"{sha}.txt"
    if p.exists():
        assert p.read_text() == text                       # collision check
    else:
        p.write_text(text)
    return sha                                             # run records store only this
```

## Pitfalls

- Generated artifacts silently drift from sections without a staleness gate — wire `find_stale()` into the test suite and offer a `--check` CLI mode, or section edits do nothing.
- Shared section files fan out: one edit can change three providers' prompts at once. Document the fan-out so reviewers know the blast radius.
- A resolver that returns its first candidate even when nothing exists hands callers phantom paths; make the loading function (not the resolver) raise `FileNotFoundError`, and never use the resolver's result without loading.
- Guidance detectors keyed to exact error strings break silently when tool error wording changes — define the strings as shared constants next to the tools that emit them.
- AST-based contract scans no-op on `SyntaxError`, i.e. exactly when the file is most broken; ensure the compile/feedback loop owns that failure mode independently.
- Decide dedup lifetime deliberately: run-lifetime signatures mean a model regressing to an already-coached mistake much later gets no second reminder; per-episode reset changes that.
- Injected guidance uses `role="user"` — fine for an autonomous single-task agent, a trust-boundary hazard in interactive agents where the model must distinguish operator from harness.
- The small-prompt/on-demand-docs design is load-bearing as a trio: index doc + "do not guess APIs" prompt rule + API-error injector. Dropping any one lets the model guess APIs blind.
- Any nondeterminism in prompt assembly (dict ordering, trailing whitespace) fragments both the reproducibility store and the server-side prompt cache — the join must be strictly deterministic.
- Two variants can be byte-identical under different filenames; tooling that assumes filename == unique content will double-count. Match by content hash.
- Autonomy language ("never ask the user", "conclude immediately after success") must be paired with hard harness stops (max turns, max cost, no-action abort); the prompt alone cannot guarantee termination.

## Checklist

- [ ] Split the prompt into ordered sections: identity/quality bar → output conventions → per-provider tool contract → domain rules.
- [ ] Declare variants as frozen data (sections tuple → output path); let providers share section files.
- [ ] Write a deterministic compiler (strip, join with `"\n\n"`, single trailing newline) and commit the generated artifacts.
- [ ] Add a staleness check to CI and a `--check` CLI mode.
- [ ] Resolve one generic prompt name per (provider, profile) at load time; explicit paths override; raise only at load, not resolve.
- [ ] Keep the system prompt ≤ ~15 KB; move documentation to the first user message, preloading only an index plus 1–2 core references.
- [ ] Put the remaining docs behind a read tool with a whitelisted virtual namespace and path-traversal rejection.
- [ ] Include anti-gaming rules ("feedback signals are sensors, not targets") and explicit termination discipline for chatty providers.
- [ ] Build a guidance injector: deterministic detectors (error-string match, output-keyword match, static scan after successful mutations), XML-tagged synthetic user messages, sha-based dedup per family.
- [ ] Implement an empty-response escalation ladder: gentle state-aware reminder → tool-naming demand with diagnostics → hard abort.
- [ ] Content-address the prompt actually used per run (sha256 store with collision check); store only the hash in run records.
- [ ] Derive the provider cache key from hashes of the full static prefix: system prompt + preloaded docs + tool schemas.
- [ ] Keep ALL prompt text — system, guidance, maintenance (e.g. compaction instructions) — in the one versioned sections directory.
