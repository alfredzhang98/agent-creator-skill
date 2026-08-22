# 14. Memory

**Maps to:** Memory · State/Context · Prompt · **Distilled from:** Claude Code 2.1.88 — `src/memdir/` (`memoryTypes.ts`, `memoryScan.ts`, `findRelevantMemories.ts`, `memoryAge.ts`, `memdir.ts`), `src/utils/claudemd.ts`, `src/utils/attachments.ts`; Articraft `storage/` (record library, BM25 retrieval)

## Why this module exists

An agent without memory re-learns the same three facts every session: who it is
working with, what was already tried, and which approach the user rejected last
week. Users notice immediately, and the workaround — pasting the same context
into every prompt — is exactly the tax memory is supposed to remove.

But memory is the subsystem where enthusiasm does the most damage. A store that
saves everything is worse than no store: it costs tokens on every turn, it goes
stale silently, and its recall precision collapses as it grows. Three questions
have to be answered before any of it works, and only the first is about
storage:

1. **What is worth saving?** Almost nothing is.
2. **What is worth recalling *now*?** Fewer things than are relevant.
3. **How do you stop a stale memory being asserted as fact?** This is the one
   that bites in production.

## How Claude Code implements it

### The rule that decides what belongs: non-derivability

The taxonomy opens by defining memory negatively — memories capture "context
NOT derivable from the current project state. Code patterns, architecture, git
history, and file structure are derivable (via grep/git/CLAUDE.md) and should
NOT be saved as memories" (`memdir/memoryTypes.ts:1-12`).

This single rule does more work than everything else in the module. Anything
the agent can rediscover in ten seconds with `grep` is a memory that will be
subtly wrong within a week and never worth the tokens. What cannot be
rediscovered at any price is what happened in a conversation nobody wrote down:
the user's constraints, the reason a decision went the way it did, the approach
that was tried and rejected.

### Four types, each answering a different question about the future

`MEMORY_TYPES = user | feedback | project | reference`
(`memdir/memoryTypes.ts:14-19`), and each carries `<when_to_save>` and
`<how_to_use>` guidance in the prompt rather than only a description
(`memoryTypes.ts:43-90`). The type is not decoration: it is what the recall
selector filters on and what tells the model how to *apply* the memory.

- **user** — role, expertise, goals. *"Collaborate with a senior software
  engineer differently than a student who is coding for the very first time."*
  With an explicit prohibition: never record anything that reads as a negative
  judgement, or that is irrelevant to the work.
- **feedback** — how to work. The instruction is unusual and correct: record
  **confirmations as well as corrections**, because *"if you only save
  corrections, you will avoid past mistakes but drift away from approaches the
  user has already validated, and may grow overly cautious"*
  (`memoryTypes.ts:60`). Corrections are loud; confirmations are quiet and get
  missed.
- **project** — ongoing work, constraints, incidents. Relative dates must be
  converted to absolute at save time — `"Thursday" → "2026-03-05"` — "so the
  memory remains interpretable after time passes" (`memoryTypes.ts:79`).
- **reference** — pointers to external resources, not copies of them.

`feedback` and `project` carry a required body structure: the fact, then a
**Why:** line and a **How to apply:** line, because "knowing *why* lets you
judge edge cases instead of blindly following the rule"
(`memoryTypes.ts:63, 81`). A rule without its reason is applied everywhere or
nowhere.

### Recall is the same three-level ladder as skills

Level 1 is a **manifest**: one line per memory —
`- [type] filename (timestamp): description` (`memdir/memoryScan.ts:84-93`) —
built by reading only each file's header, never its body. Level 2 is selection:
a **cheap-model side query** picks at most five (`memdir/findRelevantMemories.ts:18-24, 39-45`).
Level 3 is reading those five in full.

The selector prompt is where the precision lives, and two of its lines are
worth copying verbatim:

- *"If you are unsure if a memory will be useful … do not include it. Be
  selective and discerning."* Recall defaults to no.
- *"If a list of recently-used tools is provided, do not select memories that
  are usage reference or API documentation for those tools (Claude Code is
  already exercising them). DO still select memories containing warnings,
  gotchas, or known issues about those tools — active use is exactly when those
  matter."* (`findRelevantMemories.ts:23`) Relevance and *usefulness* are
  different predicates, and the difference is whether it changes what you do.

Files already surfaced in earlier turns are filtered out **before** the
selector call, so its five-slot budget is spent on fresh candidates instead of
re-picking files the caller will discard (`findRelevantMemories.ts:35-48`).

Note what this is not: there is no embedding index. The judgement being made —
"would this change what I do about *this* task" — is semantic in the task, not
in the text, and a small model reading one-line descriptions makes it better
than cosine similarity does.

### Staleness is a first-class property of every recalled memory

Age is rendered in human units — `today`, `yesterday`, `47 days ago` — because
"models are poor at date arithmetic — a raw ISO timestamp doesn't trigger
staleness reasoning the way '47 days ago' does" (`memdir/memoryAge.ts:10-14`).

Memories older than a day carry a caveat travelling *with the content*:
*"Memories are point-in-time observations, not live state — claims about code
behavior or file:line citations may be outdated. Verify against current code
before asserting as fact"* (`memoryAge.ts:33-41`). Fresh memories get no
warning at all — a caveat on this morning's note is noise, and noise is how
warnings stop being read.

The comment naming the motivating failure is the most useful sentence in the
file: users reported stale code-state memories with `file:line` citations being
asserted as fact, because **"the citation makes the stale claim sound more
authoritative, not less"** (`memoryAge.ts:29-31`). Precision and freshness are
independent, and a memory system that surfaces the first without the second
manufactures confident errors.

### The entry point is capped two ways

`MEMORY.md` is always loaded, and truncated at **200 lines AND 25,000 bytes**
(`memdir/memdir.ts:34-38`). Two caps because either alone fails: 200 long lines
were observed at 197 KB in production (`memdir.ts:36-37`), and a byte cap alone
cuts mid-line. Line-truncate first (a natural boundary), then byte-truncate at
the last newline, and name which cap fired (`memdir.ts:49-57`).

### Injection has a cumulative session budget, not just a per-turn one

Per turn: at most 5 files × 4 KB. Per session: **60 KB cumulative, after which
prefetching stops entirely** (`utils/attachments.ts:269-289`). The reasoning is
recorded inline — a per-turn cap bounds one injection, but over a long session
the selector keeps surfacing *distinct* files, "~26K tokens/session observed in
prod". Any recurring injection needs a session-level budget or it is unbounded
by construction.

The budget resets naturally at compaction, because it is computed by scanning
messages rather than tracked in a counter: old attachments are gone from
context, so re-surfacing them is valid again (`utils/attachments.ts:283-287`).

### Scope: private, team, and an explicit conflict rule

Memories are private or team-scoped, with per-type defaults: `user` is always
private; `feedback` defaults to private and goes team-wide only for genuine
project conventions ("a testing policy, a build invariant, not a personal style
preference"); `project` biases toward team (`memoryTypes.ts:40-77`). And a rule
for the collision: before saving a private feedback memory, check it does not
contradict a team one — "if it does, either don't save it or note the override
explicitly" (`memoryTypes.ts:60`).

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Define memory by **non-derivability** | Anything grep can rediscover will be stale and unread; what a conversation contained cannot be rediscovered at all | The model must judge derivability, and will sometimes save a fact the repo already states |
| Closed four-type taxonomy with per-type save/use guidance | The type drives recall filtering and tells the model how to *apply* the memory, not just what it says | A memory that fits no type is either dropped or mislabelled |
| `feedback` records confirmations, not only corrections | Saving only corrections produces an agent that avoids old mistakes but drifts from validated approaches and grows over-cautious | Confirmations are quiet; the model must be told explicitly to watch for them |
| Required **Why:** / **How to apply:** on the decaying types | The reason is what lets you judge when the rule does *not* apply | More structure to enforce; short memories feel bureaucratic |
| Absolute dates at save time | "Thursday" is unreadable in a month | Requires resolving the reference at save time, when it is still known |
| Recall = manifest → cheap-model selection → read ≤5 | Headers are cheap and the judgement is about task-relevance, not text similarity | One extra model call per turn; a bad description makes a good memory unreachable |
| No embedding index | The predicate is "would this change what I do", which similarity does not capture | Paraphrase-heavy stores may under-recall |
| Filter already-surfaced *before* selection | Otherwise the 5-slot budget is spent re-picking discarded files | Requires threading surfaced-state through the turn |
| Age in human units, caveat attached to content | Models do not do date arithmetic; a caveat in a separate message gets separated from the claim | Slightly more verbose injection |
| No caveat under two days | A warning on everything is a warning on nothing | A memory that went stale in 36 hours ships unflagged |
| Entry point capped by lines **and** bytes | Either cap alone has a documented failure mode | Two constants, two truncation messages |
| Cumulative session budget with a hard stop | Distinct-file recall is unbounded per-turn caps notwithstanding | Late-session recall silently stops; needs to be observable |
| Private vs team scope with an explicit conflict rule | A personal preference promoted to team policy is worse than not saving it | Every save is now also a scope decision |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| Memory types | `user`, `feedback`, `project`, `reference` (`memdir/memoryTypes.ts:14-19`) | Closed; unknown values degrade to untyped rather than dropping the file |
| Recall budget | ≤5 memories per selection (`memdir/findRelevantMemories.ts:20`) | Small enough that precision matters more than recall |
| Per-file injection cap | 4,096 bytes (`utils/attachments.ts:277`) | 5 × 4 KB = 20 KB bounds one turn |
| Per-file line cap | 200 lines (`utils/attachments.ts:269`) | Line cap alone does not bound size — hence the byte cap |
| Session injection cap | 60 KB, then prefetch stops (`utils/attachments.ts:288`) | ~3 full injections; ~26K tokens/session observed without it |
| `MEMORY.md` caps | 200 lines AND 25,000 bytes (`memdir/memdir.ts:34-38`) | p100 observed: 197 KB under 200 lines |
| Staleness threshold | > 1 day (`memdir/memoryAge.ts:35`) | Below it the warning is noise |
| Manifest line | `- [type] filename (timestamp): description` (`memdir/memoryScan.ts:84-93`) | Everything the selector needs; nothing it does not |

## Reusable pattern

`templates/agentkit/memory.py` is the runnable version: header scan, manifest,
selector, staleness rendering, write-side validation, session budget. The spine:

```python
NON_DERIVABILITY_RULE = (
    "Save only what cannot be recovered from the project itself. Code "
    "structure, architecture, past fixes and git history are derivable with "
    "grep and git. Save what happened in a conversation nobody wrote down.")

MEMORY_TYPES = ("user", "feedback", "project", "reference")
STRUCTURED_TYPES = {"feedback", "project"}      # these need Why / How to apply

def age_text(mtime_ms):                          # never an ISO timestamp
    d = age_days(mtime_ms)
    return "today" if d == 0 else "yesterday" if d == 1 else f"{d} days ago"

def freshness_warning(mtime_ms):                 # empty when fresh: no noise
    d = age_days(mtime_ms)
    return "" if d <= 1 else (
        f"This memory is {d} days old. Memories are point-in-time observations, "
        "not live state — file:line citations may be outdated. Verify before "
        "asserting as fact.")

def select(query, headers, choose, already_surfaced=frozenset(), k=5):
    # Filter BEFORE the call so the small budget is spent on fresh candidates.
    candidates = [h for h in headers if h.path not in already_surfaced]
    picked = set(choose(SELECTOR_PROMPT, f"{query}\n\n{manifest(candidates)}"))
    return [h for h in candidates if h.name in picked][:k]

def validate_memory(name, description, mtype, body):
    problems = []
    if mtype in STRUCTURED_TYPES and "**Why:**" not in body:
        problems.append("needs a **Why:** line — without the reason you cannot "
                        "judge when the guidance does not apply")
    if re.search(r"\b(yesterday|tomorrow|last week|next week)\b", body, re.I):
        problems.append("convert relative dates to absolute at save time")
    return problems
```

Wiring notes:

- Recall on the **user's turn**, not after tools: the query is the signal, and
  running it once per turn rather than per iteration avoids asking the selector
  the same question repeatedly.
- Prefetch it concurrently with the model call and consume the result only if
  it has settled; a memory that arrives one turn late is fine, a turn that
  waits on memory is not.
- Filter recalled memories against files the agent already read this session —
  re-injecting a file it just opened is pure duplication.

## Pitfalls

- **Saving what the repo already says.** The fastest way to build a memory
  store nobody reads. Apply the non-derivability rule at write time, not at
  review time.
- **Saving only corrections.** You get an agent that is careful and
  progressively less willing to do anything. Record what worked, too.
- **Relative dates.** "Ship by Thursday" is meaningless in a month and actively
  misleading in a year.
- **A memory without its reason.** `feedback` and `project` decay fastest and
  are applied outside their intended scope most often; the **Why** is what lets
  future-you decide the rule does not apply here.
- **Surfacing precision without freshness.** A stale `file:line` citation is
  worse than a stale vague claim — the specificity makes it sound checked.
- **Per-turn caps without a session cap.** Distinct-file recall grows without
  bound across a long session.
- **Embedding-only recall.** Similarity finds *related* memories; you want
  *decision-changing* ones. A cheap model reading descriptions is both cheaper
  and better here.
- **Re-recalling what is already in context.** Filter against surfaced paths
  and against files the agent read this session.
- **Unbounded entry point.** `MEMORY.md` grows monotonically unless capped, and
  it is loaded on every single turn.
- **No scope distinction.** A personal style preference promoted to a team
  convention is worse than not saving it at all.

## Checklist

- [ ] A written rule for what belongs, based on non-derivability
- [ ] A closed type taxonomy, with per-type save/use guidance in the prompt
- [ ] `feedback` explicitly records confirmations as well as corrections
- [ ] Decaying types require a **Why** and a **How to apply**
- [ ] Relative dates are resolved to absolute at save time
- [ ] Recall is manifest → selection → read-in-full, capped at a small number
- [ ] The selector is told to default to *no* when unsure
- [ ] Already-surfaced items are filtered before selection, not after
- [ ] Age is rendered in human units and the caveat travels with the content
- [ ] Fresh memories carry no caveat
- [ ] Both a per-turn and a cumulative per-session injection budget exist
- [ ] The always-loaded entry point is capped by lines and by bytes
- [ ] Scope (private/shared) is an explicit decision with a conflict rule
- [ ] Recall is prefetched concurrently and consumed only when settled
