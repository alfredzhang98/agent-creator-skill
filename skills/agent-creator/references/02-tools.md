# 02. Tool Design

**Maps to:** Tools · Guardrails · Evaluator/Verifier · State/Context · Cost · **Distilled from:** Articraft `agent/tools/`, `agent/harness.py:812-1030`, `agent/examples.py`

## Why this module exists

The tool layer is the agent's entire action surface: everything the LLM can do, it does through a tool call, and everything it learns about the consequences comes back as a tool result. Three failure modes dominate naive implementations: exceptions escaping tool execution break provider conversation invariants (a dangling `tool_call_id` kills the run), LLM-supplied filesystem paths create arbitrary-write risk, and unstructured error strings leave the model unable to self-correct. A disciplined tool subsystem therefore separates schema declaration, parameter validation, runtime-context binding, and execution into distinct phases, returns every failure as parseable data instead of raising, and sandboxes all mutation onto harness-chosen resources. Done right, the same tool implementations serve multiple provider APIs through thin aliases matched to each model family's training.

## How Articraft implements it

### Two-phase declarative lifecycle: Tool → Invocation → ToolResult

`BaseDeclarativeTool` holds only a name and an OpenAI-format schema plus one abstract `async build(params: dict) -> BaseToolInvocation` (`agent/tools/base.py:129-147`). `build()` validates the raw LLM dict into a strict Pydantic model (`ConfigDict(extra='forbid')`, `agent/tools/base.py:85-88`) and returns an invocation carrying typed params, a `get_description()` loggable preview, and `async execute() -> ToolResult` (`agent/tools/base.py:101-115`). `ToolResult` carries output OR error plus an optional `compilation` side-channel dict; `to_dict()` emits exactly one of `{'result': ...}` / `{'error': ...}` (`agent/tools/base.py:53-82`), which the harness JSON-serializes into the tool message (`agent/harness.py:1018-1027`). `make_tool_schema` sets `additionalProperties: false` — required for OpenAI strict mode (`agent/tools/base.py:19-50`). `validate_tool_params` strips keys whose value is explicitly `null` before validation, because several providers emit `'offset': null` for omitted optionals (`agent/tools/base.py:91-98`).

### Registry + dispatch loop: errors as data, never exceptions

`ToolRegistry` is a name→tool dict whose `get_tool_schemas()` feeds the provider request directly and whose `build_invocation()` returns `None` for unknown names (`agent/tools/registry.py:10-44`). Harness dispatch (`agent/harness.py:812-1030`): freeform "custom"-type calls map raw text to `{'input': text}` (`agent/harness.py:841`); JSON decode failures become `ToolResult(error='Invalid JSON in tool arguments: ...')`; orchestrator-owned tools are intercepted by name before registry dispatch (`agent/harness.py:881-914`); tool-specific preflights inject corrective guidance (`agent/harness.py:916-970`); runtime context is duck-type bound via `getattr(invocation, 'bind_file_path')` / `bind_virtual_workspace` (`agent/harness.py:984-989`); Pydantic `ValidationError` from `build()` is formatted as `Invalid parameters for {name}. Missing required: [...] Invalid values: [...] Provided: [...]` (`agent/harness.py:997-1013`). Every path ends in a well-formed tool message.

### Bound-resource sandbox: the LLM never names real paths

Mutating tools subclass `BoundFileToolInvocation`; the harness injects the single sandboxed target file after `build()` and before `execute()` (`agent/tools/base.py:118-126`). Skipped binding yields `ToolResult(error='file_path is required')`, not a crash (`agent/tools/write_code.py:36-37`, `agent/tools/edit_code.py:37-38`). `WriteFileTool` accepts a virtual `path` param purely for Gemini-CLI parity and rejects anything but `None` or the canonical artifact name in `build()` (`agent/tools/write_code.py:138-142`). The read tool goes further: `ReadFileInvocation` resolves paths through a bound `VirtualWorkspace` returning either in-memory content or a disk path — one namespace exposing the artifact plus read-only `docs/...` mounts regardless of physical storage (`agent/tools/read_file.py:52-64`). Output lines are prefixed `L{n}: ` (1-indexed) so later edits can reference exact lines (`agent/tools/read_file.py:90`). Because null-stripping erases provided-vs-omitted, `ReadFileTool.build` checks raw dict membership and passes `offset_provided`/`limit_provided` flags into the invocation, so range errors like "offset exceeds file length" fire only when the model actually supplied the argument (`agent/tools/read_file.py:137-144`).

### Hard vs advisory gates on every write path

`missing_required_model_contract` AST-parses candidate content and returns missing required top-level symbols — two functions plus one structural assignment checked on `ast.Assign`/`AnnAssign` (`agent/tools/model_contract.py:6-38`); on `SyntaxError` it returns `[]` so the richer syntax validator reports line detail instead. `write_code` (`agent/tools/write_code.py:39-46`) and `edit_code` — for both empty-file bootstrap (`agent/tools/edit_code.py:52-60`) and post-replacement content (`agent/tools/edit_code.py:88-95`) — refuse to write and list exactly what is missing. Separately, every mutation runs `compile(code, filename, 'exec')` and attaches `{'status','error','error_line'}` as `ToolResult.compilation` — but the write proceeds even on syntax error (`agent/tools/write_code.py:48-55`): losing the domain contract makes every downstream check meaningless (hard block), while a syntax error is exactly what the self-correction loop fixes next turn (advisory).

### Exact-string edit with uniqueness enforcement

`EditCodeParams = {old_string, new_string, replace_all=False}` (`agent/tools/edit_code.py:18-138`). Empty `old_string` is legal only on an empty file (bootstrap, `agent/tools/edit_code.py:44-64`); a missing `old_string` returns "Make sure the string matches exactly, including whitespace and indentation" (`agent/tools/edit_code.py:66-71`); multiple occurrences without `replace_all` return the count and ask for a longer unique snippet or `replace_all=true` (`agent/tools/edit_code.py:73-80`). The harness adds two preflights that pre-read the file because models repeatedly hit exactly these modes: empty `old_string` on a non-empty file → "Call read_file(...) and retry"; non-empty `old_string` on an empty file → "Initialize it with write_file(...)" (`agent/harness.py:916-970`).

### One parser, two transports: grammar-constrained freeform + JSON patch

Two tool wrappers share `ApplyPatchInvocation`. The freeform variant declares a non-function schema `{'type':'custom', 'format':{'type':'grammar','syntax':'lark',...}}` so OpenAI Responses constrained decoding cannot emit a syntactically malformed patch (`agent/tools/apply_patch.py:20-39, 105-127`); the JSON variant wraps the identical parser in a normal function schema with one `input` string for providers lacking freeform tools (`agent/tools/apply_patch.py:130-156`). The parser enforces a strict envelope, rejects Add/Delete/Move hunks (single-file sandbox), and requires `@@` headers with ` `/`+`/`-` prefixed lines (`agent/tools/apply_patch.py:159-232`). Hunk application rejects hunks with no context/removal lines as ambiguous insertions, searches for the old block starting after the previous hunk with fallback to file start, and preserves trailing-newline state (`agent/tools/apply_patch.py:235-271`).

### Orchestrator-owned stub tools

`CompileModelTool` advertises a zero-parameter schema whose description tells the model when to call it and what the harness auto-checks, so the model does not re-author baseline checks (`agent/tools/compile_model.py:17-59`). Its `execute()` returns `ToolResult(error='compile_model must be handled by the harness')` — a deliberate tripwire that should never run. The harness intercepts by name before registry dispatch, rejects unexpected parameters, then runs the real compile/QC which mutates loop state the tool layer cannot see (compile-freshness tracking, checkpoint persistence) (`agent/harness.py:881-914`). Pattern: expensive stateful operations stay in the schema list as ordinary tools but execute inside the orchestrator.

### Provider-shaped tool surfaces with delegating aliases

`build_tool_registry` gives each provider the tool names/shapes its model family was RL-trained on: OpenAI gets the grammar freeform patch tool; Codex-CLI gets JSON patch + `replace` + `write_file`; everyone else (Anthropic/Gemini/etc.) gets `replace` + `write_file` (`agent/tools/__init__.py:77-112`). All aliases delegate to one shared implementation: `ReplaceInvocation` maps its params onto `EditCodeParams`, constructs an `EditCodeInvocation`, re-binds the file path, and delegates (`agent/tools/edit_code.py:159-167`); `WriteFileInvocation` maps `content`→`code` onto `WriteCodeInvocation` the same way (`agent/tools/write_code.py:98-102`). One behavior, N provider idioms.

### Retrieval tool: weighted BM25 + heuristic re-rank + tiered gating

`find_examples` searches curated markdown docs with YAML frontmatter, indexed per domain pack with `lru_cache(maxsize=8)` (`agent/examples.py:444-454`). Field weighting is implemented by token repetition in the BM25 bag: slug 6x, title 5x, tags 4x, description 3x, code identifiers 2x, prose 1x (`agent/examples.py:39-46`). BM25 over-fetches `k = min(N, max(limit*6, 12))` candidates (`agent/examples.py:706`), then each score gains exact/prefix/phrase field bonuses, `coverage^2 * 3.0`, and structured-match bonuses, with morphological alias hits at 0.3x (`agent/examples.py:466-569`). Gating classifies results into strong vs weak tiers: single-token queries need structured (metadata) signal or high per-field thresholds (`agent/examples.py:587-611`); the weak tier is capped at `min(limit, 2)` and labeled `match_quality='weakly_relevant'`, and the tool description tells the model to treat those as inspiration-only. The harness deduplicates full-document payloads across turns, replacing seen content with a placeholder plus `content_skipped=True` (`agent/harness.py:781-799`).

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Errors returned as `ToolResult(error=...)` data, never raised; `ValidationError` formatted as Missing/Invalid/Provided | The LLM is the error consumer; a corrective string lets it self-repair, while an exception aborts the loop or dangles a `tool_call_id` | Genuine harness bugs masquerade as LLM-visible errors; needs separate operator logging, and badly written error strings make the model loop |
| Two-phase build/execute with duck-typed `bind_*` runtime injection | Validation stays pure and testable; tools stay stateless singletons; `get_description()` previews before side effects; dispatcher needs no per-tool knowledge | Binding is not type-enforced — a missed bind surfaces only at runtime as a recoverable LLM error |
| Bound-resource sandbox; virtual `path` params exist only for CLI-signature parity | Eliminates path traversal / arbitrary-write risk entirely; keeps task framing simple | Single-resource only; multi-file work needs a workspace abstraction; the patch tool must explicitly reject Add/Delete/Move |
| Per-provider tool surfaces mimicking each family's native tooling, aliases delegating to shared invocations | Models perform best with tool names/shapes they were RL-trained on; grammar-constrained decoding makes malformed patches impossible | Registry assembly branches on provider; alias parity is maintained by hand; dispatcher needs a custom-vs-function path |
| Syntax check advisory (write anyway, report in side channel); domain-contract check hard-blocking | Blocking on syntax discards large otherwise-good edits; losing the entrypoint contract makes all later steps meaningless | File can be transiently broken between turns; in Articraft the patch tool skips the contract check — an inconsistency to fix in your build |
| Orchestrator-owned tools as schema-only stubs intercepted by name | Verification must mutate loop state (freshness, checkpoints) invisible to the tool layer, yet appear to the LLM as an ordinary tool | Dispatch special case; the stub's error is only a tripwire if wiring breaks |
| Null-valued params stripped before strict validation; raw-dict flags where provided-vs-omitted matters | Providers emit `null` for omitted optionals; stripping keeps `extra='forbid'` schemas strict without spurious failures | Null and absent become indistinguishable, forcing awkward `*_provided` side flags |
| Lexical BM25 + hand-tuned bonuses + tiered gating instead of embeddings | Deterministic, dependency-light, instant; metadata fields predict relevance far better than prose; labeled weak tier beats silent noise | Many corpus-tuned magic thresholds; paraphrased queries can miss; requires cross-turn payload dedupe |
| `ToolResult` carries a validation side channel distinct from output/error | A successful write can simultaneously report success and a machine-parsed syntax status without conflating success with validity | Field is write-tool specific; consumers must tolerate its absence |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| `additionalProperties` | `false` (`agent/tools/base.py:46`) | Required for OpenAI strict-mode function schemas; pairs with `extra='forbid'` |
| Description preview length | 50 chars (80 for patches) (`agent/tools/write_code.py:29`, `agent/tools/apply_patch.py:58`) | One-line mutation preview for logs/TUI |
| Required contract symbols | 2 functions + 1 structural assignment (`agent/tools/model_contract.py:16-18`) | Hard invariant every write must satisfy for downstream verification to work |
| `find_examples` limit default | 3 (`agent/tools/find_examples.py:19`) | Full documents are returned; small default protects context |
| BM25 candidate pool | `min(N, max(limit*6, 12))` (`agent/examples.py:706`) | Over-fetch before re-rank and gating prune |
| Field repetitions | slug 6, title 5, tags 4, desc 3, code 2, prose 1 (`agent/examples.py:39-46`) | Field weighting via token repetition in the corpus bag |
| Exact-match bonuses | 4.0 / 3.5 / 3.0 / 1.75 / 1.25 / 0.5 by field (`agent/examples.py:47-54`) | Metadata hits outweigh prose hits additively on top of BM25 |
| Coverage bonus | `coverage^2 * 3.0`; structured +2.0/+1.0 (`agent/examples.py:543-548`) | Superlinear reward for matching many distinct query tokens |
| Alias-token factor | 0.3x, metadata fields only (`agent/examples.py:529-531`) | Morphological variants count at reduced weight |
| Prefix-match minimum | 4 chars both sides (`agent/examples.py:343-351`) | Prevents short-token prefix noise |
| Score retention | strong ≥ `max(1.0, best*0.5)`; weak ≥ `max(1.0, best*0.7)`, cap `min(limit,2)` (`agent/examples.py:610, 652, 682-683`) | Drops long tails; weak tier hard-capped and labeled |
| Index caches | `lru_cache(maxsize=8)` per domain corpus (`agent/examples.py:444, 454`) | Build once per process per corpus |
| Patch constraints | exactly one Update block; Add/Delete/Move rejected; ` `/`+`/`-` line prefixes (`agent/tools/apply_patch.py:178-231`) | Single bound file; ambiguous insertions rejected (`apply_patch.py:252-255`) |
| Image limits | Gemini < 20MB, others ≤ 50MB; per-provider mime whitelist (`agent/tools/__init__.py:26-64, 208-215`) | Provider inline payload limits, validated before the run starts |

## Reusable pattern

```python
"""Declarative tool framework: schema -> validated invocation -> data-only results.
Stdlib only. Swap 'artifact' and 'verify' for your domain's resource and checker."""
import json


class ToolResult:
    def __init__(self, output=None, error=None, side_channel=None):
        self.output, self.error, self.side_channel = output, error, side_channel

    def to_dict(self):  # exactly one of result|error -> trivially LLM-parseable
        d = {"error": self.error} if self.error else {"result": self.output}
        if self.side_channel:
            d["validation"] = self.side_channel  # e.g. advisory syntax status
        return d


class ParamError(Exception):
    def __init__(self, missing=(), invalid=(), provided=()):
        self.missing, self.invalid, self.provided = missing, invalid, provided


def validate_params(spec, raw):
    """spec: {name: (type, required, default)}. Strict + null-stripping."""
    raw = {k: v for k, v in raw.items() if v is not None}  # null == omitted
    extra = [k for k in raw if k not in spec]
    missing = [k for k, (_, req, _) in spec.items() if req and k not in raw]
    invalid = [k for k, v in raw.items()
               if k in spec and not isinstance(v, spec[k][0])]
    if extra or missing or invalid:
        raise ParamError(missing, invalid + extra, list(raw))
    out = {k: raw.get(k, d) for k, (_, _, d) in spec.items()}
    out["_provided"] = set(raw)  # provided-vs-omitted survives null-stripping
    return out


class Invocation:
    def __init__(self, params): self.params, self.resource = params, None
    def bind_resource(self, handle): self.resource = handle  # harness injects
    def describe(self): return "<preview before side effects, <=50 chars>"

    async def execute(self):
        if self.resource is None:
            return ToolResult(error="resource not bound")  # tripwire, not crash
        try:
            return await self._run()
        except Exception as exc:  # NEVER let exceptions escape
            return ToolResult(error=f"Tool execution error: {exc}")

    async def _run(self):
        code = self.params["content"]
        gaps = missing_domain_contract(code)      # HARD gate: structural/AST
        if gaps:
            return ToolResult(error=f"Refusing to write; missing: {gaps}")
        status = advisory_check(code)             # ADVISORY: lint/compile
        self.resource.write(code)                 # write even if status failed
        return ToolResult(output="written", side_channel=status)


class Tool:
    name, schema, spec = "write_artifact", {...}, {...}  # additionalProperties:false
    async def build(self, raw):                   # phase 1: pure, testable
        return Invocation(validate_params(self.spec, raw))


REGISTRY = {t.name: t for t in tools_for(provider)}  # per-provider surface:
# give each family the names/shapes it was trained on; aliases remap params
# and delegate to one shared Invocation. Grammar-constrained freeform where
# supported; a JSON string param wrapping the same parser elsewhere.
ORCHESTRATOR_OWNED = {"verify"}  # schema-only stubs the loop intercepts by name


async def dispatch(call, ctx):
    try:
        args = ({"input": call.text} if call.kind == "freeform"
                else json.loads(call.arguments))
    except json.JSONDecodeError as e:
        return tool_msg(call, ToolResult(error=f"Invalid JSON in arguments: {e}"))
    if call.name in ORCHESTRATOR_OWNED:           # stateful verify: runs in the
        return tool_msg(call, await ctx.run_owned(call))  # loop, mutates state
    tool = REGISTRY.get(call.name)
    if tool is None:
        return tool_msg(call, ToolResult(error=f"Tool {call.name} not found"))
    try:
        inv = await tool.build(args)
    except ParamError as e:
        return tool_msg(call, ToolResult(
            error=f"Invalid parameters for {call.name}. Missing: {e.missing} "
                  f"Invalid: {e.invalid} Provided: {e.provided}"))
    for hook in ("bind_resource", "bind_workspace"):  # duck-typed injection
        fn = getattr(inv, hook, None)
        if callable(fn):
            fn(ctx[hook])
    return tool_msg(call, await inv.execute())


def tool_msg(call, result):  # every path ends in a well-formed tool message
    return {"role": "tool", "tool_call_id": call.id,
            "content": json.dumps(result.to_dict())}
```

## Pitfalls

- Never raise out of tool execution: bad JSON args, unknown tool, validation failure, and runtime crash must all still produce a well-formed tool message, or the dangling `tool_call_id` breaks the provider conversation.
- Strip explicit-`null` params before strict validation (providers emit them for omitted optionals) — then recover provided-vs-omitted from raw dict membership wherever error messages depend on it, or range errors misfire on defaults.
- Reserve hard write blocks for domain invariants whose loss makes downstream steps meaningless; make syntax/lint checks advisory (write anyway, attach status), or you discard the model's work and stall the loop.
- Apply gates uniformly across ALL write paths: Articraft's patch tool skips the contract check that the rewrite and edit tools enforce, so a patch can strip required entrypoints undetected until verification. Route every mutation through one shared gate function.
- Exact-string edits must enforce uniqueness (report occurrence count, offer `replace_all` and "use a longer snippet") and restrict empty `old_string` to empty-file bootstrap, or models silently corrupt files. Add dispatcher preflights for the failure modes models actually repeat.
- Write error strings as corrective actions ("Call read_file(...) then retry with a smaller exact snippet"), not failure descriptions — the model executes them verbatim.
- Freeform grammar tools deliver raw text, not JSON: the dispatcher must map `{'input': text}` itself and tag results so the codec emits the provider's custom-output message type.
- Reject patch hunks with no context/removal lines (ambiguous insertions); match hunks starting after the previous hunk before falling back to file start; preserve trailing-newline state.
- Duck-typed `bind_*` hooks make a missed binding a runtime LLM-visible error, not a static failure — keep the "resource not bound" tripwire message distinctive so wiring bugs are obvious in traces.
- Raw BM25 over a small corpus returns confident-looking noise: the tiered gating, the explicit `weakly_relevant` label echoed in the tool description, and the hard cap of 2 weak results are all load-bearing.
- Retrieval tools that return full documents blow the context budget on repeat calls — deduplicate by document id across turns in the orchestrator, replacing seen content with a skipped placeholder.
- Give orchestrator-owned stub tools an `execute()` that returns a loud error ("must be handled by the harness") so miswired interception is visible instead of a silent no-op.

## Checklist

- [ ] `ToolResult.to_dict()` emits exactly one of `result`/`error`, plus an optional validation side channel
- [ ] Every `execute()` body wrapped so no exception ever escapes; validation errors formatted as Missing/Invalid/Provided
- [ ] Schemas strict: `additionalProperties: false` + reject-extras validation; explicit nulls stripped pre-validation
- [ ] Provided-vs-omitted flags computed from the raw params dict where messages depend on the distinction
- [ ] Mutating tools operate only on harness-bound resources; LLM-supplied paths validated against the sandbox namespace
- [ ] One hard structural gate (domain contract) shared by ALL write paths; syntax/lint advisory with status attached
- [ ] Exact-string edit enforces uniqueness with `replace_all` escape hatch; empty match string only bootstraps empty files
- [ ] Dispatcher preflights added for the corrective loops your models actually hit (log and mine transcripts)
- [ ] Per-provider tool surface assembled from aliases that delegate to shared invocations; alias params strictly constrained
- [ ] Grammar-constrained freeform variant where the provider supports it; JSON wrapper over the same parser elsewhere
- [ ] Stateful/expensive operations advertised as tools but intercepted by name and executed in the orchestrator
- [ ] Read tool returns 1-indexed line-anchored output (`L{n}: `) so edits can cite exact lines
- [ ] Retrieval results tiered, weak tier capped and labeled, tool description explains the label; payloads deduped across turns
- [ ] `get_description()`-style previews (~50 chars) available before side effects, for logs and traces
