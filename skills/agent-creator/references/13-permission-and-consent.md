# 13. Permission & Consent

**Maps to:** Guardrails · Tools · Executor · **Distilled from:** Claude Code 2.1.88 — `src/utils/permissions/permissions.ts`, `src/types/permissions.ts`, `src/tools/BashTool/` (bashPermissions, bashSecurity, readOnlyValidation, pathValidation), `src/utils/sandbox/sandbox-adapter.ts`

## Why this module exists

Reference 03 says success must be verified rather than self-reported, and
assumes a mechanical check exists. For open-ended work — "improve this code",
"draft this migration", "clean up the config" — it does not. There is no
compiler for *better*.

When the verifier cannot be the authority that says no, **the human is**. A
permission system is what turns that from a slogan into a mechanism. Its
quality is not measured by how much it blocks; it is measured by how *rarely it
has to ask* while still catching everything that matters. An agent that asks
about every read is ignored within a day, and an agent that never asks is
turned off after its first `rm -rf`.

The whole design problem is therefore consent economics: make the common case
free, make the dangerous case unmissable, and make the grant reusable at the
right scope.

## How Claude Code implements it

### Two layers, and reading only the inner one is a trap

`hasPermissionsToUseTool` (`utils/permissions/permissions.ts:473-520`) is the
outer half; `hasPermissionsToUseToolInner` (`:1158-1319`) is the ladder below.
The split is deliberate and the source says why — mode transformations are
applied *after* the ladder "so it can't be bypassed by early returns". The
`dontAsk` transformation lives there, and it converts `ask` into **deny**.

That direction is the whole point and it is easy to get backwards. `dontAsk` is
not "bypass lite": it is the most *restrictive* autonomous mode, chosen so an
unattended run never blocks on a prompt. A one-pass implementation that folds
mode handling into the ladder will, sooner or later, convert `ask` into
`allow` — which is how "skip the prompts" silently becomes "grant everything".

### The inner ladder, every step labelled

`hasPermissionsToUseToolInner` (`utils/permissions/permissions.ts:1158-1319`)
is a numbered sequence, and the comments in the source use those numbers:

| Step | Check | Outcome |
|---|---|---|
| 1a | blanket deny rule for the tool | **deny** |
| 1b | blanket ask rule for the tool | **ask** (unless a sandbox auto-allow applies) |
| 1c | `tool.checkPermissions(input)` | the tool's own opinion |
| 1d | tool said deny | **deny** |
| 1e | tool said ask **and** `requiresUserInteraction()` | **ask** |
| 1f | tool said ask because of an explicit content-scoped rule | **ask** |
| 1g | tool said ask because of a safety check | **ask** |
| 2a | mode is `bypassPermissions` | **allow** |
| 2b | explicit allow rule for the tool | **allow** |
| 3 | anything left, including `passthrough` | **ask** |

Every decision carries a `decisionReason` naming the step and the rule that
produced it, which is what makes a surprising outcome debuggable instead of
mystical — and what lets the audit log say *why*, not just *what*.

### Steps 1d–1g are bypass-immune, and that is the whole design

`bypassPermissions` sits at step **2a** — *after* four classes of decision that
it therefore cannot override: an explicit deny, a tool-level deny, a call that
inherently needs a human, and a safety check on a sensitive path
(`utils/permissions/permissions.ts:1238-1260`). The comments state the intent directly: a
content-scoped ask rule the user configured "must be respected even in bypass
mode, just as deny rules are respected at step 1d".

This is the difference between a mode and a master key. "Skip the prompts" is a
statement about *convenience*, not about *authority*, and a permission system
where the convenience flag disables the safety rules has no safety rules.

### `passthrough` → `ask`: unknown means ask

A tool with no opinion returns `passthrough`, and step 3 converts it to `ask`
(`utils/permissions/permissions.ts:1299-1310`). There is no code path where silence means yes.
The default in `buildTool` is deliberately the *only* non-conservative default
in that table (`Tool.ts:762-766`) — and it is safe precisely because it
delegates here rather than short-circuiting.

### Rules are scoped, and the dialog teaches scoping

A rule is `{tool, behavior, content?}`. Bare `Bash` matches the whole tool;
`Bash(git status)` matches exactly; `Bash(git:*)` matches a prefix
(`SkillTool.ts:451-467` shows the same matcher for skills). When the ladder
lands on `ask`, the dialog offers *both* the exact and the prefix grant as
one-click rules with an explicit destination
(`tools/SkillTool/SkillTool.ts:540-567`). A dialog whose only affordance is
"always allow this tool" trains people to grant far more than they meant to.

### Isolation buys autonomy

`autoAllowBashIfSandboxed` defaults to **true**
(`utils/sandbox/sandbox-adapter.ts:471`): a command that will run inside the OS
sandbox skips the prompt entirely, and this is wired in at step 1b so it even
overrides a standing ask rule — but only for commands that genuinely *will* be
sandboxed (`utils/permissions/permissions.ts:1186-1206`, `tools/BashTool/bashPermissions.ts:1356`).

This inverts the usual framing. The sandbox is not a tax on the agent's
capability; it is what pays for the agent's independence. Every additional
thing you can safely isolate is a class of question you never have to ask
again — which is the only sustainable way to reduce prompt fatigue without
reducing safety.

### The cost of an open-world action space, measured

To decide whether a shell command is read-only, which paths it touches, and
whether it is destructive, Claude Code carries **~8,500 lines of permission,
security and command-parsing logic out of ~12,400 in `src/tools/BashTool/`**
(`bashPermissions.ts` 2,621 + `bashSecurity.ts` 2,592 +
`readOnlyValidation.ts` 1,990 + `pathValidation.ts` 1,303), plus a 4,436-line
shell parser in `utils/bash/bashParser.ts`.

That is the true price of an arbitrary-shell escape hatch, and it is a
*standing* correctness risk, not a one-time cost: anything the parser
mis-classifies is mis-permissioned. Reference 10's argument — constrain the
action space — is not aesthetics. It is this number.

### Convenience filters are labelled as such

`containsExcludedCommand` carries the comment: *"excludedCommands is a
user-facing convenience feature, not a security boundary. It is not a security
bug to be able to bypass excludedCommands"* (`tools/BashTool/shouldUseSandbox.ts:18-20`).
Naming a filter honestly in the source is what stops the next maintainer from
loading it with weight it cannot carry — and stops a user from configuring it
as if it were a control.

### Denials are counted

Repeated denials are tracked per session and per async subagent, and crossing a
limit converts silent denial into an explicit ask
(`utils/permissions/permissions.ts:959-1058`). An agent grinding against a wall it cannot see is
burning budget; the denial counter is what notices.

### Failing loudly when a security setting cannot be honoured

If the user explicitly enabled the sandbox but it cannot run, the system
returns a human-readable reason rather than silently continuing unsandboxed —
the fix for a bug where "isSandboxingEnabled() silently returned false … giving
users zero feedback that their explicit security setting was being ignored",
called out in the source as *"a security footgun"*
(`utils/sandbox/sandbox-adapter.ts:550-556`).

This is the general correction to Articraft's known sharp edge (unknown model
pricing silently disabling the cost cap): **a guard that cannot run must say
so.** Note the milder variant used for cost, where accuracy rather than safety
is at stake: an unknown model falls back to a default price, sets a flag, and
the total is annotated *"costs may be inaccurate due to usage of unknown
models"* (`utils/modelCost.ts:166-172`, `cost-tracker.ts:228-233`). Refuse when
a safety property is unmet; estimate-and-flag when only precision is.

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| One ordered ladder with numbered, named steps | A decision you cannot explain is one you cannot audit or debug | The order encodes policy; reordering two steps silently changes behaviour |
| Bypass mode sits *after* deny/interaction/safety checks | "Skip prompts" is convenience, not authority; a mode that disables safety rules leaves you with none | Users who wanted true YOLO still get prompts and may seek a worse workaround |
| `passthrough` → `ask` | Unknown must never mean yes | Every new tool prompts until someone writes a rule; onboarding feels chatty |
| Rules carry optional content scope with prefix matching | `git status` and `git push` are different grants; `git` alone is not useful | Prefix rules are coarse: `git:*` covers `git push --force` |
| The ask dialog offers exact **and** prefix grants | Otherwise people click "always allow this tool" and lose all granularity | Two similar buttons; the scope difference must be legible |
| Sandboxed commands auto-allow by default | Isolation is what buys autonomy; without this the sandbox only adds friction | Everything now depends on `willBeSandboxed()` being exactly right |
| Per-input predicates rather than per-tool | `ls` and `rm -rf` are the same tool; treating them alike is either useless or unusable | Requires classifying arbitrary input — see the 8,500-line number |
| Read-only classification is an **allowlist** | An unknown binary must not be auto-approved because nobody listed it as dangerous | New safe commands prompt until added |
| Denial counting escalates to an explicit ask | An agent grinding on a wall it cannot see wastes the whole budget | Another threshold to tune |
| Refuse when a safety setting cannot run; estimate-and-flag when only accuracy suffers | Silent security downgrades are the worst failure mode; silent *inaccuracy* is merely bad | Two behaviours to explain |
| Convenience filters are named as non-boundaries in the source | Stops the next maintainer, and the user, from relying on them | Requires discipline; the naming is the only enforcement |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| Permission modes | `plan`, `default`, `acceptEdits`, `dontAsk`, `bypassPermissions` (+ internal `auto`, `bubble`) (`types/permissions.ts:16-38`) | NOT a single autonomy ladder: `plan` is read-only, `bypass` skips prompts but still respects 1a-1g, and `dontAsk` is the most restrictive — it turns every `ask` into a `deny` |
| Bypass-immune steps | 1d, 1e, 1f, 1g (`utils/permissions/permissions.ts:1225-1260`) | Tool deny, required interaction, content-scoped ask, safety check |
| Default for a tool with no opinion | `passthrough` → `ask` (`utils/permissions/permissions.ts:1299-1310`) | The only default; there is no path where silence means yes |
| `autoAllowBashIfSandboxed` | default **true** (`sandbox-adapter.ts:471`) | Isolation buys autonomy; sandboxed commands skip the prompt |
| Rule scopes | `Tool`, `Tool(exact)`, `Tool(prefix:*)` (`SkillTool.ts:451-467`) | Three granularities people actually reason about |
| Rule sources | policy, user, project, local, flag, cliArg, command, session (`types/permissions.ts:54-62`) | Provenance drives both precedence and the audit label |
| Shell permission surface | ~8,500 of ~12,400 lines in `src/tools/BashTool/` | The measured price of an open-world action space |
| Classifier fail-closed refresh | 30 min (`utils/permissions/permissions.ts:107`) | How long a failed classifier stays fail-closed before retry |

## Reusable pattern

`templates/agentkit/permissions.py` is the runnable version. The spine:

```python
BYPASS_IMMUNE = {"rule_deny", "rule_ask", "safety"}

def decide(tool, input_, ctx, context, rule_key=None):
    if r := ctx.matching("deny", tool.name, rule_key):      # 1a
        return Permission("deny", reason={"type": "rule_deny", "step": "1a", "rule": r})
    if r := ctx.matching("ask", tool.name, None):           # 1b
        return Permission("ask", reason={"type": "rule_ask", "step": "1b", "rule": r})

    try:                                                    # 1c
        result = tool.check_permissions(input_, context)
    except Exception as exc:
        # A broken predicate must not become an implicit allow.
        result = Permission("passthrough", reason={"type": "error", "detail": str(exc)})

    if result.behavior == "deny":                                        # 1d
        return result
    if result.behavior == "ask" and tool.requires_user_interaction():    # 1e
        return result
    if result.behavior == "ask" and result.reason.get("type") in BYPASS_IMMUNE:
        return result                                                    # 1f/1g

    if ctx.mode == "bypass":                                # 2a — AFTER the immune set
        return Permission("allow", reason={"type": "mode", "step": "2a"})
    if r := ctx.matching("allow", tool.name, rule_key):     # 2b
        return Permission("allow", reason={"type": "rule_allow", "step": "2b", "rule": r})

    return Permission("ask", suggestions=suggest_rules(tool.name, rule_key))  # 3
```

Two supporting pieces do most of the real work:

```python
# Read-only classification is an ALLOWLIST, applied to EVERY part of a
# compound command. `ls && rm -rf /` must never inherit `ls`'s verdict.
def is_read_only(command):
    parts = split_on(command, "&&", "||", "|", ";", "\n")
    return bool(parts) and all(known_read_only(p) for p in parts)

# The grant the human is offered must be scopeable, or they will pick the
# widest option available.
def suggest_rules(tool, key):
    return ({"tool": tool, "content": key,               "scope": "exact"},
            {"tool": tool, "content": f"{head(key)}:*",  "scope": "prefix"})
```

## Pitfalls

- **Putting bypass mode first.** If "skip prompts" is checked before deny
  rules, you do not have a permission system, you have a suggestion box.
- **Treating a mode as a master key.** Some decisions must survive every mode:
  explicit denies, calls that inherently need a human, and safety checks on
  sensitive paths.
- **Blocklisting dangerous commands.** The dangerous thing you did not list is
  the one that runs. Allowlist what is safe; assume the rest writes.
- **Classifying a compound command by its first part.** `ls && rm -rf /` is not
  read-only. Split first, and let the worst part decide.
- **Forgetting redirection and in-place flags.** `echo x > f`, `sed -i`,
  `find -delete` are writes wearing read-only clothing.
- **Offering only "always allow this tool".** People click it. Offer the narrow
  grant first and make the scope legible.
- **A prompt per read.** Asking about safe, frequent, reversible actions
  destroys the user's attention for the one prompt that mattered. Auto-allow
  reads, and pay for the rest with isolation.
- **Silently downgrading when a guard cannot run.** If the sandbox was
  requested and is unavailable, say so and stop. Reserve estimate-and-flag for
  cases where only accuracy is at stake.
- **Convenience filters that read like controls.** Name them in the source, or
  someone will configure their security posture around one.
- **No denial counter.** An agent retrying a denied action forever looks fine
  in the logs and empties the budget.

## Checklist

- [ ] One ordered ladder; every decision returns the step and rule that caused it
- [ ] Bypass mode is evaluated *after* deny, required-interaction, content-scoped ask, and safety checks
- [ ] A tool with no opinion produces `ask`, never `allow`
- [ ] A tool whose permission predicate raises produces `ask`, never `allow`
- [ ] Rules support tool / exact / prefix scopes and carry their source
- [ ] The ask dialog offers a narrow and a broad grant, with the scope visible
- [ ] Safety predicates are functions of the input, not the tool
- [ ] Read-only classification is an allowlist, applied per part of compound input
- [ ] Sandboxed execution auto-allows, and `willBeSandboxed()` is exact
- [ ] Blanket-denied tools are filtered out of the pool before the request is built
- [ ] Repeated denials escalate rather than repeating silently
- [ ] A safety setting that cannot be honoured refuses loudly; accuracy-only gaps estimate and flag
- [ ] Convenience filters are documented as non-boundaries at their definition
