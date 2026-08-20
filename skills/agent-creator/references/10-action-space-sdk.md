# 10. Shaping the Action Space with a Domain SDK

**Maps to:** Tools · Guardrails · Evaluator/Verifier · State/Context · SystemPrompt · Skills · **Distilled from:** Articraft `sdk/_core/v0/`, `sdk/_profiles.py`, `sdk/__init__.py`, `sdk/_extensions/cadquery/v0/cadquery.py`

## Why this module exists

If the agent emits the target format directly (XML, SQL, YAML manifests, raw API calls), errors surface only at parse or execution time, with feedback that names line numbers instead of the agent's own concepts. A domain SDK inverts this: the agent writes one script against a small declarative vocabulary, and everything downstream — validation, derived artifacts, geometric/semantic QC, the final format emitter — is owned by the harness. The SDK then becomes the entire action space: what it cannot express, the agent cannot break; what it validates eagerly, the agent hears about in its own terms. Crucially, the SDK is also the feedback channel — every error message is engineered as input to the next self-correction iteration, so message design is prompt design. Articraft applies this to articulated 3D objects (URDF is the target format), but the pattern transfers to any domain where an LLM authors structured artifacts.

## How Articraft implements it

### Declarative builder with dual-vocabulary aliases

`ArticulatedObject` exposes `model.part(...)`, `model.articulation(...)`, `model.material(...)` that append to lists and maintain name indexes (`sdk/_core/v0/articulated_object.py:71-232`). Parent/child parameters accept either an object or a name string via `_part_name_ref` (`articulated_object.py:42-52`), so the agent never juggles IDs. Every domain term has an industry-standard alias: `.links`/`.joints` properties (`articulated_object.py:91-104`), `link()`/`joint()` methods (`articulated_object.py:126-139,180-205`), `joint_type`/`limit`/`dynamics` aliases on `Articulation` (`sdk/_core/v0/types.py:373-395`), `Cylinder(height=)` and `Part.visual(color=)` with mutual-exclusion `TypeError`s (`Material(color=)` likewise rejects `rgba`+`color` together, raising `ValidationError`). An LLM trained on the standard corpus cannot misname the API — both spellings work.

### Eager validate() with named-entity errors

`validate(strict=True)` checks name uniqueness, referential integrity, parent≠child, single-parent rule, per-relation-kind contracts (revolute/prismatic must carry lower+upper limits; continuous must not; effort/velocity > 0), mimic/derived-value compatibility with DFS cycle detection that prints the cycle path, and positive dimensions (`articulated_object.py:278-463,532-601`). `_validate_connectivity` requires exactly one root and BFS-reachability of all parts, listing the exact unreachable parts (`articulated_object.py:482-517`). Every message embeds the offending entity name in repr form — a direct token the LLM can grep in its own code.

### Compiler-owned derived outputs (agents cannot author collisions)

`validate()` rejects any agent-authored `Part.collisions` unless a private flag is set (`articulated_object.py:449-455`). `compile_object_model_with_exact_collisions` deep-copies the model, sets the flag on the copy, and derives one collision per visual 1:1 (`sdk/_core/v0/exact_collisions.py:23-137`). The compiled result is memoized on the source model keyed by sha256 of a canonical JSON serialization plus the asset root plus `_CACHE_VERSION=7` (`exact_collisions.py:140-153`) — repeated QC compiles once, any edit invalidates. This deletes an entire silent-error class (visual/collision desync) from the action space.

### Self-verification vocabulary: never-throwing check recording

`TestContext` composes three mixins over the model, a seed, and ~12 memo caches (`sdk/_core/v0/_testing/context.py:14-52`). All checks funnel through `_record(name, ok, details)` which accumulates and returns the bool — never raises (`sdk/_core/v0/_testing/core.py:65-71`); `_record_warning_check` is the non-blocking twin (`core.py:72-83`). Check names auto-derive from the call signature (`expect_gap(lid,base,axis=z)`). `report()` freezes everything into an immutable report with failures, warnings, and allowances (`core.py:43-63`). Because checks record instead of raising, one run surfaces ALL failures — maximum information per self-correction iteration. Only structural model invalidity raises `ValidationError` (measurement on an invalid model is meaningless).

### Exact assertions plus temporary-state posing

`expect_contact`/`expect_gap`/`expect_overlap`/`expect_within` measure real geometry (FCL collide/distance, per-geometry support functions, vertex projection for meshes) with element-level scoping via named visuals (`sdk/_core/v0/_testing/expectations.py:105-365`; measurement kernels `core.py:344-478`). Every failure detail embeds measured value, threshold, element names, and current pose. `with ctx.pose({joint: value})` validates the joint, refuses posing mimic-driven joints, coerces the value per joint type, and invalidates world-transform/AABB/element caches on both enter and exit (`core.py:560-606`). One forward-kinematics function powers both agent-facing queries and all QC sweeps (`sdk/_core/v0/geometry_qc.py:766-871`), with mimic chains resolved recursively with cycle detection (`geometry_qc.py:893-947`).

### Deterministic QC sweeps with two severity tiers

`_joint_sample_values` gives each joint a small decisive set — fixed→[0], continuous→[0, ±π/2, π], limited→[0, lower, upper, mid] — capped at 32 author-overridable samples (`geometry_qc.py:983-1031`). `generate_pose_samples` enumerates the cartesian product when it fits, else zero pose → one-hot perturbations → `random.Random(seed)` fill, deduped (`geometry_qc.py:1034-1100`). Structural checks split into blocking `fail_if_*` (isolated parts via FCL contact-graph connected components, overlaps with AABB prefilter + FCL confirm, disconnected geometry islands — `sdk/_core/v0/_testing/model_checks.py:236-651`, `geometry_qc.py:1104-1226,1596-1673`) and advisory `warn_if_*` (coplanar z-fighting heuristic with low/medium/high risk tiers — `model_checks.py:653-948`). Hollow meshes are split into connected components (`__component_NNN` suffix) before FCL so shells never read as solids (`geometry_qc.py:542-631`).

### Justified exceptions (allow_*)

`allow_overlap(a, b, reason=..., elem_a=..., elem_b=...)` raises `ValueError` on an empty reason and records the allowance three ways: pair-key dict for matching, human-readable string in the report, and a scoped tuple (`core.py:94-124,480-558`). Allowed-but-detected findings still emit warnings and structured entries in the report — exceptions are auditable, never silent, and downstream graders can penalize blanket allowances. Alias stripping keeps element-scoped allowances working across mesh component splitting (`core.py:94-106`).

### Actionable failure prose with computed fix hints

The overlap validator raises ONE worst-case error containing pair, elements, depths, pose, then a computed suggestion — it picks the least-overlapping axis, signs it by relative centers, and says "hint: try moving <link> by ~0.0234m along world z", followed by an honesty caveat about heuristic conservatism (`geometry_qc.py:1752-1816`). Motion-axis failures append a right-hand-rule hint: "If motion is reversed, flip the joint axis sign" (`expectations.py:764-769`). Deprecation is a steering channel: legacy helpers warn once, naming the exact replacement (`core.py:322-339`), and `sdk/__init__.py` `__getattr__` turns removed names into guidance-bearing `AttributeError`s (`sdk/__init__.py:14-19`). Multi-finding failures truncate to 8-12 preview rows with "... (N more)" so feedback stays token-bounded (`model_checks.py:78-79,124-125,402-403`).

### Semantic helpers replace hand-tuned low-level values

`place_on_face(parent, '+y', ...)` maps face strings to (axis, sign, rpy) via a table so the agent never writes rotation trig (`sdk/_core/v0/placement.py:407-586`); `place_on_surface` handles curved geometry with exact nearest-point normals (`placement.py:1659-1704`). `Mesh` carries optional `source_geometry`/`source_transform` provenance so a generated mesh that is "really a box" is measured analytically instead of by vertex iteration (`types.py:81-121`, `sdk/_core/v0/_mesh_provenance.py:12-100`).

### Content addressing everywhere

Managed assets: an ambient `AssetSession` in a contextvar lets agent code reference meshes by logical name only — no absolute paths ever appear in agent scripts, keeping records relocatable (`sdk/_core/v0/assets.py:20-24,68-98,415-438`). Files dedupe on (name, sha256(payload)) with digest-suffix escalation at 12/16/20/64 chars before erroring (`assets.py:216-289`). Expensive CAD tessellation caches on sha256 of (schema version, tessellation params + library version, binary serialization of the shape itself) with an AABB metadata sidecar (`sdk/_extensions/cadquery/v0/cadquery.py:387-427,498-600`) — hashing built content, not source code, means unchanged parts hit the cache even after unrelated edits, so recompile latency stays proportional to the diff.

### Deterministic target-format emitter, stdlib-only

`compile_object_to_urdf_xml` validates, optionally swaps in the compiled-collisions model, then emits XML with `xml.etree.ElementTree` alone (`sdk/_core/v0/_urdf_export.py:28-61`). Output is normalized for diff-stability: floats via `%.6g`, identity origins omitted, axes unit-normalized with a 1e-12 guard, default mimic attributes elided (`_urdf_export.py:64-92,184-263`). Deterministic emission makes target-format snapshots comparable across agent iterations. Docs say "Do not emit URDF XML directly" — `to_urdf()` exists only as a compatibility shim.

### Domain pack: profile, versioned facade, docs contract

`SdkProfile` is a frozen dataclass bundling everything domain-specific: package name, scaffold path (starter script), `docs_full`/`docs_core` tuples mounted read-only into the agent workspace, and one system-prompt filename per provider (`sdk/_profiles.py:16-59,104-122`). `docs_for_mode` selects full (26 docs) / core (4 docs) / none — a context-budget dial (`_profiles.py:29-36,62-114`). Retargeting the agent to a new domain is registering a new profile; the harness consumes only this interface. The public namespace is a version alias (`sdk` → `sdk.v0` → `sdk._core.v0`) with lazy `__getattr__` export so `import sdk` stays cheap despite heavy dependencies (`sdk/__init__.py:1-29`); generated code and persisted trajectories keep compiling against v0 when a v1 ships. Doc topic filenames deliberately do not look like importable module paths, defended in three places against `from sdk.testing import ...` confusion.

## Design decisions

| Decision | Rationale | Tradeoff |
|---|---|---|
| Constrain the agent to a typed declarative DSL; never let it emit the target format directly | Typed objects validate eagerly with entity-named messages, compile with derived artifacts, and QC geometrically; raw output fails only at parse time with poor feedback | SDK must reimplement domain semantics (FK, mimic resolution) the downstream format engine already has; DSL-omitted features are inexpressible |
| Forbid agent-authored derived artifacts (collisions); compiler derives them 1:1 behind a private flag | Visual/collision desync is a large silent-error class for LLMs; derivation guarantees QC and the consumer see the same thing | No hand-optimized coarse shells; every QC pass pays exact cost (mitigated by content-hash caching) |
| Checks record into a report and return bool; only structural invalidity raises | One run surfaces all assertion failures — maximum signal per iteration; measurement on an invalid model is meaningless so that fails fast | Agent can ignore returned bools and compute on garbage mid-test |
| Two-tier severity (`fail_if_*` / `warn_if_*`) plus runtime deprecation naming the exact replacement | Warnings steer the policy (and future training data) toward exact checks without breaking the existing generated-script corpus | Old helpers linger; two names exist for many concepts |
| Exceptions require a justified `allow_*` with non-empty reason, element scoping, report echo, and warnings when fired | Real mechanisms genuinely need waivers; a required reason makes the agent commit to an auditable rationale; scoping stops blanket waivers masking new bugs | A plausible-sounding reason can still silence a real defect; relies on downstream review |
| Clamp agent-supplied tolerances (truncate + hard cap); relaxations need a reason and warn | Prevents the agent from making checks vacuous with huge tolerances; env-var defaults let the harness tune strictness fleet-wide | Hidden clamping can surprise a legitimately large-scale model; absolute tolerances are not scale-aware |
| Determinism everywhere: seeded sampling, content-hash compile cache, sha256-addressed assets | The self-correction loop only works if the same model fails the same way twice; content addressing makes reruns idempotent on disk | Seed-fixed sampling can systematically miss failing regions; cache-version bumps needed on semantics changes |
| Docs are part of the action-space contract, tiered via the profile (full/core/none), mounted read-only | On-demand focused reference pages keep prompt cost down while giving exact signatures instead of hallucinations | Docs and code can drift; needs an explicit docs-update-with-behavior-change policy |
| Single asset format (.obj only) across assets, QC, and collision derivation | One parseable text format lets the SDK read vertices directly and build collision structures without a format matrix | No textures/richer formats in the source pipeline; conversion pushed to import time |
| Public namespace is a version alias with lazy export and curated errors for renamed symbols | Generated code and persisted trajectories reference the public name forever; versioned internals allow breaking changes as v1 | Three indirection layers hurt grep/jump-to-definition; star re-exports can widen the surface silently |
| Cache expensive builds on content hash (params + library version + binary shape), not source code | Refactors that don't change output still hit the cache; library upgrades correctly invalidate | Key computation requires serializing the object; version-in-key nukes the whole cache on upgrades |

## Constants that matter

| Constant | Value | Why this number |
|---|---|---|
| Overlap tolerance | 0.005 m (env-overridable) — `geometry_qc.py:1819-1827` | Minimum per-axis penetration before a pair is even FCL-checked; filters flush-mount micro-contact noise |
| Overlap volume tolerance | 5e-7 m³ — `geometry_qc.py:1829-1837` | Second prefilter so sliver intersections don't fail the blocking overlap pass |
| Contact tolerance | 1e-6 m — `geometry_qc.py:1840-1848` | Separation below which parts count as touching for support graphs and contact assertions |
| Joint-origin tolerance | 0.015 m default, hard cap 0.15 m, truncated to 3 decimals — `_testing/common.py:37-39,226-231` | Cap + truncation stops agents neutralizing the check with giant tolerances |
| Pose sample budgets | 256 default / 128 overlap / 32 coplanar / 1 isolated; per-joint override cap 32 — `geometry_qc.py:1037`, `model_checks.py:239,423,656` | Bounds QC cost; zero + one-hot + seeded-random ordering covers decisive configurations first |
| Continuous-joint samples | [0, -π/2, +π/2, π] — `geometry_qc.py:1015-1016` | Fixed quarter-turn probes where limits can't supply bounds |
| Failure preview truncation | 8-12 rows + "... (N more)" — `model_checks.py:78-79,124-125,402-403` | Error text is designed to fit an LLM context while stating total counts |
| Derived-artifact cache version | `_CACHE_VERSION = 7` — `exact_collisions.py:20` | Version salt in the sha256 key; bump invalidates all cached derivations when semantics change |
| Asset digest lengths | 12, then 16/20/64 on collision — `assets.py:34-35,216-289` | Content-addressed dedup with graceful escalation before hard error |
| uv_margin clamp | [0, 0.49] per axis — `placement.py:554-555` | Guarantees face placements stay strictly inside the face despite careless margins |
| Mesh component suffix | `__component_%03d`, stripped when matching allowances — `geometry_qc.py:542-552`, `core.py:94-106` | Split components stay traceable to the authored name so scoped waivers survive representation changes |
| Docs tiers | full = 26 pages, core = 4, none = 0 — `_profiles.py:62-114` | Token-budget dial over mounted authoring reference |
| Tessellation cache | tolerance 0.001, angular 0.1, schema "v1" in key — `cadquery.py:23,161-166,498-505` | Fidelity/size tradeoff; params live in the cache key so changing them invalidates correctly |
| Emitter float format | `%.6g`, identity origins omitted, 1e-12 guards — `_urdf_export.py:64-92,184-263` | Deterministic, compact, diffable target-format output across iterations |

## Reusable pattern

```python
# Typed action-space SDK + self-verification harness, any domain.
# The agent writes ONE script: build_model() + run_tests(); the harness
# compiles, runs QC, and feeds every failure string back as the next prompt.
import hashlib, json, random
from contextlib import contextmanager
from dataclasses import dataclass, field

class ValidationError(Exception): pass

class Model:                                  # the declarative action space
    def __init__(self): self.entities, self.relations = [], []
    def entity(self, name, **props):          # domain noun: part/account/page
        e = {"name": name, **props}; self.entities.append(e); return e
    def relation(self, name, kind, parent, child, **props):
        ref = lambda x: x["name"] if isinstance(x, dict) else x   # obj OR name
        r = {"name": name, "kind": kind, "parent": ref(parent),
             "child": ref(child), **props}
        self.relations.append(r); return r
    # Alias every industry-standard synonym onto these methods so a model
    # trained on the standard corpus cannot misname the API.

    def validate(self, strict=True):
        # Fail fast; NAME the entity in every message (a greppable token):
        #  - unique names; referential integrity; parent != child; single parent
        #  - per-kind contracts (required props, positive magnitudes)
        #  - exactly one root; BFS reachability -> list unreachable entities
        #  - derived-value cycles via DFS, printing the cycle path
        names = {e["name"] for e in self.entities}
        for r in self.relations:
            if r["parent"] not in names:
                raise ValidationError(
                    f"Relation {r['name']!r} references missing parent {r['parent']!r}")

def compile_model(model, _cache={}, CACHE_VERSION=1):
    """Harness-owned derivation. Agents may NOT author derived artifacts."""
    model.validate(strict=True)
    key = hashlib.sha256((json.dumps(
        {"e": model.entities, "r": model.relations}, sort_keys=True)
        + f"|v{CACHE_VERSION}").encode()).hexdigest()
    if key not in _cache:                     # content hash, not source hash
        _cache[key] = derive_checked_artifacts(model)
    return _cache[key]

class TestContext:                            # agent-facing verification
    def __init__(self, model, seed=0):        # seed => deterministic sweeps
        self.model, self.seed = model, seed
        self.checks, self.failures, self.warnings = [], [], []
        self._allow, self._allowances, self._state = [], [], {}

    def _record(self, name, ok, details=""):  # NEVER raises: collect ALL
        self.checks.append(name)
        if not ok: self.failures.append((name, details))
        return bool(ok)

    def expect_measure(self, a, b, *, threshold, elem_a=None, name=None):
        # Exact assertion: auto-named; details embed measured vs threshold
        # AND current state — everything the LLM needs to fix its script.
        measured = exact_measure(self.model, a, b, scope=elem_a)
        return self._record(
            name or f"expect_measure({a},{b})", measured <= threshold,
            f"measured={measured:.4g} threshold={threshold:.4g} "
            f"state={self._state} hint: {fix_hint(a, b, measured, threshold)}")

    def fail_if_disconnected(self, *, tol=None):     # blocking structural QC
        tol = clamp_tolerance(tol if tol is not None
                              else env_default("CONTACT_TOL", 1e-6))
        findings = [f for f in connected_components_vs_root(self.model, tol)
                    if not self._is_allowed(f)]
        preview = "\n".join(map(format_finding, findings[:10]))   # token-bounded
        more = "" if len(findings) <= 10 else f"\n... ({len(findings)-10} more)"
        return self._record("fail_if_disconnected()", not findings, preview + more)

    def allow_exception(self, a, b, *, reason, elem_a=None, elem_b=None):
        # Justified waiver: reason REQUIRED, scoped, auditable, still warns.
        if not reason.strip():
            raise ValueError("allow_exception requires a non-empty reason")
        self._allow.append((tuple(sorted((a, b))), elem_a, elem_b))
        self._allowances.append(f"allow_exception({a!r},{b!r}): {reason}")

    def _is_allowed(self, finding):
        key = tuple(sorted(finding["pair"]))
        hit = any(k == key for k, *_ in self._allow)
        if hit: self.warnings.append(f"finding allowed by justification: {key}")
        return hit

    @contextmanager
    def state(self, overrides):               # temporary pose/config override
        prev, self._state = self._state, coerce_and_validate(self.model, overrides)
        invalidate_caches(self)               # on enter AND exit — both matter
        try: yield
        finally: self._state = prev; invalidate_caches(self)

    def sweep_states(self, max_samples=256):  # deterministic: zero -> one-hot
        rng = random.Random(self.seed)        # -> seeded random fill, deduped
        return generate_state_samples(self.model, rng, max_samples)

    def report(self):
        return {"passed": not self.failures, "checks": list(self.checks),
                "failures": list(self.failures), "warnings": list(self.warnings),
                "allowances": list(self._allowances)}   # waivers stay visible

@dataclass(frozen=True)
class DomainProfile:                          # the pluggable domain pack
    package_name: str
    scaffold_path: str                        # starter script the agent edits
    docs_full: tuple = ()                     # mounted read-only in workspace
    docs_core: tuple = ()                     # cheap tier for tight contexts
    prompt_by_provider: dict = field(default_factory=dict)
    def docs_for_mode(self, mode):            # context-budget dial
        return {"full": self.docs_full, "core": self.docs_core, "none": ()}[mode]

PROFILES = {}                                 # new domain == one new entry;
                                              # harness consumes ONLY this API.
def emit_target_format(model):
    """Deterministic stdlib-only emitter: normalized floats ('%.6g'),
    defaults omitted, so snapshots diff cleanly across agent iterations.
    The agent never calls this — docs say 'do not emit the format directly'."""

# Error-message design rules (this is the feedback channel to the LLM):
#  - name the exact entity/element; embed measured vs threshold + current state
#  - compute a fix hint ("try moving 'lid' by ~0.023 along z"); admit
#    heuristic conservatism honestly so the agent doesn't chase false positives
#  - clamp agent-supplied tolerances (truncate + hard cap); relaxations need a
#    reason and emit a warning
#  - deprecate via runtime warning naming the exact replacement, never by
#    breaking; turn removed names into guidance-bearing AttributeErrors
```

## Pitfalls

- Agents neutralize checks with huge tolerances — clamp hard (Articraft truncates joint-origin tolerance to 3 decimals and caps at 0.15 m) and require a reason plus a warning for any relaxation.
- Blanket waivers mask real bugs — require element-level scoping and non-empty reasons on every `allow_*`, echo allowances in the report, and still warn when a waiver actually fires.
- Letting the agent author derived artifacts (collisions, indexes, caches) invites silent desync — derive them in a compiler pass gated by a private flag the agent cannot set.
- Throwing on the first failed assertion starves the self-correction loop — record and return bool; reserve exceptions for structural invalidity where further measurement is meaningless.
- Unbounded failure text blows the LLM context — truncate every multi-finding message to ~10 preview rows with an explicit "... (N more)" total.
- Stale caches after temporary-state overrides silently measure the wrong state — invalidate on both enter AND exit of the context manager.
- Non-determinism breaks iteration stability — seed all sampling, order samples deterministically (zero → one-hot → seeded random), and content-address every cache and asset.
- Names in test scripts are an implicit contract — once `run_tests()` references element names, renames break checks; document this, and alias-strip any internal suffixes (Articraft's `__component_NNN`) when matching waivers.
- Doc topic names that look like module paths get imported (`from sdk.testing import ...`) — pick non-importable filenames and add curated `__getattr__` errors that name the correct import.
- Hashing source code instead of built content for expensive-build caches misses the common case — hash the serialized object plus params plus library version, so refactors hit and upgrades invalidate.
- Legacy coarse checks give false confidence — keep them working but runtime-deprecate with the exact replacement named, steering future generations toward exact checks.
- Version the public namespace from day one — generated scripts and persisted trajectories reference your API forever; a `pkg → pkg.v0 → pkg._core.v0` alias chain lets v1 ship without breaking them.

## Checklist

- [ ] Agent authors ONE script against a declarative builder; the target format is emitted only by a harness-owned, deterministic, stdlib-only compiler
- [ ] Every builder parameter accepts object OR name string; every industry-standard synonym is aliased with mutual-exclusion TypeErrors
- [ ] `validate(strict=True)` runs eagerly and names the offending entity (repr form) in every message; graph checks list exact unreachable nodes and print cycle paths
- [ ] Derived artifacts are compiler-owned behind a private flag; compilation is memoized on a content hash that includes a bumpable cache version
- [ ] Test checks record into a report and return bool — one run surfaces all failures; only structural invalidity raises
- [ ] Assertion failure details embed measured value, threshold, entity/element names, current state, and a computed fix hint with an honesty caveat
- [ ] Two severity tiers (`fail_if_*` blocking, `warn_if_*` advisory) plus deprecation warnings naming exact replacements
- [ ] `allow_*` waivers require non-empty reasons, support scoping, appear in the report, and warn when fired
- [ ] Agent-supplied tolerances are clamped (cap + truncation); defaults come from env vars with code fallbacks
- [ ] All sampling is seeded and deterministically ordered; same model fails the same way twice
- [ ] Multi-finding feedback is truncated with explicit "(N more)" counts to fit an LLM context
- [ ] Semantic helpers replace hand-tuned low-level values (no trig/offsets in agent code); provenance metadata lets generated artifacts stay analytically measurable
- [ ] Assets are content-addressed by logical name + sha256; no absolute paths in agent scripts
- [ ] Domain knowledge lives behind one frozen profile dataclass (scaffold, tiered docs, per-provider prompts); retargeting = registering a new profile
- [ ] Public namespace is a lazy versioned facade; removed symbols raise guidance-bearing errors
