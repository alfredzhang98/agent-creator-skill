# Templates

Stdlib-only Python skeletons distilled from the Articraft agent
(`Articraft/src/articraft/`). Each file is syntactically valid, typed, and
domain-neutral: swap in your own artifact type, checks, and transport.
Start with `agent_loop.py`; the other files plug into its Protocol ports.

| File | What it gives you | Reference doc |
| --- | --- | --- |
| `agent_loop.py` | The turn loop: dual counters (turns vs llm_calls), two-stage no-action escalation then abort, finish gate requiring a fresh verify, cost-cap checks before and after each LLM call, tool errors returned as messages | `references/01-agent-loop.md` |
| `tools.py` | Declarative tool base: param specs, JSON-schema generation, build/execute invocation lifecycle, resource binding, registry + dispatch, every failure as a `ToolResult` | `references/02-tools.md` |
| `verifier.py` | Structured signal vocabulary (failure/warning/note), bundle rendering for the LLM with primary-issue selection and response rules, revision-keyed verify cache with repeated-failure streaks | `references/03-evaluator-verifier.md` |
| `sandbox_runner.py` | Untrusted-code execution in a fresh subprocess: POSIX rlimits (cpu/memory/fds), timeout kill-and-reap, one JSON result pipe with typed in-band errors | `references/04-sandboxed-execution.md` |
| `provider_adapter.py` | Minimal provider seam: `complete(messages, tools) -> {text, tool_calls, usage}`, codec protocol, env key rotation, thinking-level mapping table, jittered retries, dry-run payload preview | `references/05-providers.md` |
| `cost_meter.py` | Pricing table (cached vs uncached input, cache writes, output), per-turn accumulation, separate maintenance ledger, hard cap that aborts BEFORE the next call, budget override cascade | `references/07-cost-guardrails.md` |

Not templated (see the reference docs directly): prompts-as-code
(`06-prompts.md`), state persistence (`08-state-persistence.md`),
run orchestration (`09-orchestration.md`), and the typed action-space SDK
(`10-action-space-sdk.md`).
