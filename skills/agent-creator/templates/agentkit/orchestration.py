"""Running agents: lifecycle, staged failure, delegation.

Adapted from Articraft `agent/single_run.py` and `agent/rerun.py` (Apache-2.0)
for the run lifecycle, and Claude Code 2.1.88
`src/tools/AgentTool/loadAgentsDir.ts` for the declarative subagent contract.

Two problems live here, and they are less alike than they look.

**Running one agent to completion** is about where failure happened. "It
didn't work" is useless in a batch of two hundred; "the agent gave up",
"the agent finished but the artifact failed verification", and "everything
worked but the write failed" need different responses, so they get different
exit codes and different treatment of the staging directory.

**Delegating to another agent** is about capability. A subagent that inherits
its parent's approvals is a privilege-escalation path, and one that inherits
its parent's full instruction set is a bill nobody reads.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Any, Callable, Iterable, Sequence

from state import Paths, RunMetadata, Staging, Transcript, sortable_id, write_json


class Exit(IntEnum):
    """Staged exit codes. The stage IS the diagnosis.

    A single non-zero code makes a batch of failures unsortable. These let you
    ask "how many runs produced an artifact that failed verification" without
    reading a log.
    """

    OK = 0
    USAGE = 1            # bad invocation; nothing was attempted
    AGENT = 2            # the agent stopped without producing an artifact
    VERIFY = 3           # an artifact exists and did not pass
    PERSIST = 4          # verified, but committing it failed


@dataclass
class Outcome:
    exit_code: Exit
    run_id: str
    detail: str = ""
    #: Where the work lives. On failure this is the staging directory, kept
    #: deliberately: a failed run you cannot inspect is a failed run twice.
    artifact_path: str | None = None
    turns: int = 0
    cost_usd: float = 0.0
    stop_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == Exit.OK


@dataclass
class RunSpec:
    """Everything needed to reproduce a run.

    Kept separate from the run itself so it can be persisted, diffed, and
    replayed. A run whose inputs live only in the shell history that launched
    it is not reproducible, whatever the transcript says.
    """

    task: str
    model: str = ""
    max_turns: int = 40
    max_cost_usd: float | None = None
    #: Free-form, but persisted: code version, config hashes, seeds. Whatever
    #: you would need to explain a result to yourself in six months.
    provenance: dict[str, Any] = field(default_factory=dict)
    #: Set when this run derives from another (see `fork`).
    parent_run_id: str | None = None
    seed_artifact: str | None = None


def run_once(
    spec: RunSpec,
    root: str,
    *,
    execute: Callable[[RunSpec, Paths, Transcript], tuple[str, dict[str, Any]]],
    verify: Callable[[str], tuple[bool, str]] | None = None,
    promote_to: Callable[[str], str] | None = None,
    now: Callable[[], float] = time.time,
) -> Outcome:
    """Stage, run, verify, promote — with the failure stage recorded.

    *execute* returns ``(artifact_path, stats)``; it does the model work and
    knows nothing about persistence. *verify* is the gate, if the domain has
    one. *promote_to* maps a run id to its final home.

    The ordering is the design: nothing is committed until it is verified, so
    the durable store only ever contains work that passed. A crash leaves a
    staging directory rather than something indistinguishable from a success.
    """
    run_id = sortable_id("run-")
    paths = Paths(root, run_id).ensure()
    meta = RunMetadata(
        session_id=run_id, status="running", started_at=now(),
        cwd=paths.session_dir, model=spec.model, description=spec.task[:200],
        provenance={**spec.provenance,
                    "parent_run_id": spec.parent_run_id,
                    "seed_artifact": spec.seed_artifact},
    )
    meta.save(paths.metadata)
    staging = Staging(paths)

    def finish(code: Exit, detail: str, artifact: str | None,
               stats: dict[str, Any] | None = None) -> Outcome:
        stats = stats or {}
        meta.status = "completed" if code == Exit.OK else "failed"
        meta.ended_at = now()
        meta.stop_reason = detail[:200]
        meta.turns = int(stats.get("turns", 0))
        meta.cost_usd = float(stats.get("cost_usd", 0.0))
        # Persist on EVERY exit path, including the failures. A cost ledger
        # that only survives success answers the least interesting question.
        meta.save(paths.metadata)
        return Outcome(code, run_id, detail, artifact,
                       meta.turns, meta.cost_usd, detail[:80])

    with Transcript(paths.transcript) as trace:
        trace.append("run_start", {"spec": spec.task[:500], "model": spec.model})
        try:
            artifact, stats = execute(spec, paths, trace)
        except Exception as exc:  # noqa: BLE001
            trace.append("run_error", {"error": f"{type(exc).__name__}: {exc}"})
            return finish(Exit.AGENT, f"{type(exc).__name__}: {exc}", staging.abandon())
        if not artifact:
            return finish(Exit.AGENT, "the agent produced no artifact",
                          staging.abandon(), stats)

        if verify is not None:
            # Re-verify out of band even when the agent claims success. The
            # agent's claim is evidence, not proof, and this is the last
            # place to catch a confident lie cheaply.
            passed, why = verify(artifact)
            trace.append("verify", {"passed": passed, "detail": why})
            if not passed:
                return finish(Exit.VERIFY, why, staging.abandon(), stats)

        if promote_to is None:
            return finish(Exit.OK, "completed", artifact, stats)
        try:
            final = staging.promote(promote_to(run_id))
        except Exception as exc:  # noqa: BLE001
            # Verified work that could not be written is its own category:
            # the artifact is good, the storage is broken, and retrying the
            # agent would be the wrong response.
            trace.append("persist_error", {"error": str(exc)})
            return finish(Exit.PERSIST, f"{type(exc).__name__}: {exc}", staging.abandon(), stats)
        return finish(Exit.OK, "completed", final, stats)


def fork(parent: RunSpec, parent_run_id: str, *, task: str | None = None,
         seed_artifact: str | None = None, **overrides: Any) -> RunSpec:
    """Derive a new run from an existing one.

    A fork that starts from the parent's artifact is an *edit*; one that starts
    from scratch with the same inputs is a *rerun*. The distinction is carried
    by `seed_artifact` rather than by convention, because the two produce very
    different trajectories and mixing them poisons any dataset built from the
    traces.
    """
    return replace(
        parent,
        task=task or parent.task,
        parent_run_id=parent_run_id,
        seed_artifact=seed_artifact,
        **overrides,
    )


def batch(specs: Sequence[RunSpec], root: str, *, runner: Callable[[RunSpec], Outcome],
          results_path: str | None = None) -> list[Outcome]:
    """Run many, record every outcome, never stop on failure.

    Writes one row per run. In a batch, the failures are the dataset — a
    harness that aborts on the first one has thrown away the reason you ran a
    batch.
    """
    outcomes: list[Outcome] = []
    for spec in specs:
        try:
            outcomes.append(runner(spec))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(Outcome(Exit.AGENT, "unknown", f"harness error: {exc}"))
    if results_path:
        write_json(results_path, [
            {"run_id": o.run_id, "exit_code": int(o.exit_code),
             "exit": o.exit_code.name, "detail": o.detail,
             "turns": o.turns, "cost_usd": o.cost_usd}
            for o in outcomes
        ])
    return outcomes


def summarise(outcomes: Iterable[Outcome]) -> dict[str, int]:
    """Counts by exit stage — the number you actually want from a batch."""
    counts: dict[str, int] = {e.name: 0 for e in Exit}
    for o in outcomes:
        counts[o.exit_code.name] += 1
    return counts


# --------------------------------------------------------------------------
# Delegation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentDefinition:
    """A subagent, declared rather than coded.

    New agent types become files, not commits. The fields that matter most are
    the restrictive ones: what this agent may NOT do, and what it does NOT
    inherit.
    """

    agent_type: str
    when_to_use: str
    system_prompt: str
    #: Positive allowlist. Empty means "inherit the parent pool, minus denied".
    tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    model: str = ""
    max_turns: int = 20
    #: Run in an isolated working copy so parallel agents cannot conflict.
    isolation: str = ""            # "" | "worktree"
    #: Skip the project instruction hierarchy. Read-only agents rarely need
    #: commit or lint guidance, and at fleet scale that omission is the single
    #: largest per-spawn saving available.
    omit_project_instructions: bool = False


#: Never available to a subagent, whatever its definition says.
#: Each exclusion carries its reason inline, because a list of names decays
#: into folklore within two refactors.
UNIVERSAL_SUBAGENT_DENY: dict[str, str] = {
    "Agent": "recursion — a subagent spawning subagents has no natural bound",
    "AskUserQuestion": "there is no user attached to a delegated run",
    "ExitPlanMode": "plan mode is a main-thread abstraction",
    "TaskStop": "needs main-thread task state it cannot see",
}


def resolve_subagent_tools(
    definition: AgentDefinition,
    parent_pool: Sequence[str],
    *,
    extra_deny: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Compute a subagent's tool set. Returns (allowed, {denied: why}).

    Allowlists **replace** rather than extend. Inherited consent is the
    subtlest escalation path in a delegating agent: the parent's "yes, run the
    build" must not silently become the child's standing grant.
    """
    deny = {**UNIVERSAL_SUBAGENT_DENY, **(extra_deny or {})}
    for name in definition.disallowed_tools:
        deny.setdefault(name, f"excluded by the {definition.agent_type} definition")
    base = list(definition.tools) if definition.tools else list(parent_pool)
    allowed = [t for t in base if t not in deny]
    refused = {t: deny[t] for t in base if t in deny}
    return allowed, refused


@dataclass
class Delegation:
    """A typed result, so the caller can tell an answer from a handle.

    Returning prose for both means the parent has to parse English to find out
    whether the work is done — which it will get wrong exactly when the child
    failed.
    """

    status: str                     # "completed" | "launched" | "failed"
    agent_type: str
    agent_id: str
    result: str = ""
    output_path: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    error: str = ""

    @classmethod
    def completed(cls, agent_type: str, agent_id: str, result: str, **kw: Any) -> "Delegation":
        return cls("completed", agent_type, agent_id, result=result, **kw)

    @classmethod
    def launched(cls, agent_type: str, agent_id: str, output_path: str) -> "Delegation":
        return cls("launched", agent_type, agent_id, output_path=output_path)

    @classmethod
    def failed(cls, agent_type: str, agent_id: str, error: str) -> "Delegation":
        return cls("failed", agent_type, agent_id, error=error)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in ("", 0, 0.0)}
