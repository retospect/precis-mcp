# 0056 — Chemistry & protein tool-packs as plugins behind a canonical `route` kind

- **Status**: proposed (2026-07-14) · the two enabling core seams are
  **built + shipped + deployed** (main `c13281e1`); the tool-packs
  themselves are not sliced yet. This ADR records the *decisions*; the
  full architecture, engine map, and build order live in
  [`docs/design/chem-tools-integration.md`](../design/chem-tools-integration.md).
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0044 — the derived-job lane](./0044-derived-job-lane.md) — every
    heavy engine run (a retrosynthesis search, an AlphaFold fold, a DFT
    relax) is a derived, content-addressed, cache-fillable `kind='job'`
    parented on the artifact it produces. Chem/protein compute re-uses
    this lane unchanged.
  - [ADR 0043 — the `structure` kind](./0043-structure-kind-atomistic-ir.md),
    [0042 — `pcb`](./0042-pcb-kind-netlist-placement-ir.md),
    [0041 — `cad`](./0041-cad-kind-analytic-ir.md) — the keystone-kind
    discipline this generalizes: *own a legible IR, rent the heavy kernel
    only at job time; the LLM traverses a graph, never pixels.* A `route`
    is the retrosynthesis analogue of a `structure`/`cad`/`pcb` IR.
  - [ADR 0007 — derived queue, no block jobs](./0007-derived-queue-no-block-jobs.md)
    — the idempotent content-addressed philosophy the route cache obeys
    (same target + engine digest + stock snapshot ⇒ zero recompute).
  - **catpath integration** ([`docs/design/catpath-integration.md`](../design/catpath-integration.md))
    — the first plugin tool-pack (`pathway` kind); its `can_own_jobs`
    spine is the first of the two seams this ADR relies on.

## Context

There is a large, growing set of external chemistry/biology compute
tools worth reaching: retrosynthesis planners (AiZynthFinder, ASKCOS),
route analysis (LinChemIn), agentic layers (ChemCrow), protein structure
prediction (AlphaFold, already GPU-installed on spark), and sequence
design (ProteinMPNN/RFdiffusion). The obvious industry pattern — a broker
MCP fronting one MCP server per capability domain — is wrong *for precis*:
it fragments the agent's tool surface into per-engine servers, the exact
sprawl the seven-verb design exists to avoid, and it treats MCP as the
chemistry engine rather than as the protocol boundary.

precis already **is** the facade. The seven verbs + `kind=` are the stable
boundary; the compute lane (0044) is the off-request-path execution
substrate; the job/provenance kinds are the audit store. What was missing
was the ability to add a whole capability domain **without editing core** —
so each new tool is additive, swappable, and shippable dark.

## Decision

1. **No broker / per-engine MCP servers.** Each external tool becomes a
   **kind** (the legible IR the LLM reads) + a **`job_type` executor** (the
   heavy engine, run on a compute node via the 0044 derived-job lane).
   The verb surface never changes when an engine is added or swapped.

2. **One canonical `route` kind.** AiZynth, ASKCOS, and future planners
   normalize to a single retrosynthesis route-graph IR; **LinChemIn is the
   normalizer, run at route-ingest** (the Marker analogue) so "swap the
   engine, keep the schema" is enforced in one place. `protein` (AlphaFold)
   and `sequence` (design) are sibling kinds on the same substrate. Not
   per-engine kinds; not folded into catpath's `pathway`.

3. **Tool-packs are plugins, not core kinds.** Each domain snaps in via
   entry points and ships dark behind a flag, exactly like catpath's
   `pathway`. Core stays lean; adding the *next* tool-pack touches no core
   code.

4. **Two enabling core seams — both landed (main `c13281e1`).** These are
   the only core changes the plugin model needs:
   - **`KindSpec.can_own_jobs`** — a plugin kind may own compute-lane jobs
     without a core edit to `JOB_PARENT_KINDS`.
   - **Open relation vocabulary** — `validate_relation(store=…)` reads the
     live `relations` table (`Store.valid_relations()`, cached), so a
     plugin seeds its own link relations (`consumes`/`produces`,
     `pathway-node`) in its migration and uses them without editing the
     closed `Relation` literal. The DB FK stays the durable guard; the
     literal stays the built-in typo-safety hint.

5. **Engines run on Linux compute nodes; the Macs orchestrate only.**
   Two engine styles, split by cost: **GPU-native in-process** on spark
   (AlphaFold/DFT/MACE, installed by an ansible role) vs **portable-CPU
   container** (AiZynth/ASKCOS/LinChemIn/ChemCrow, a thin wrapper
   `FROM upstream@digest`). Containers are **built on demand** per node
   (`podman build` at deploy) — **no tarball store, no registry** until
   fleet size forces one; **model weights mount from the NAS**, not baked
   into the image. The container-runtime install is shared with the
   `sandbox_run` substrate (ADR 0048).

6. **Repo split by portability.** Shareable artifacts (plugin code,
   wrapper Dockerfiles, a generic compose file) live in **precis-mcp**;
   fleet-private glue (inventory, the capability→node `topology.yml`,
   secrets, roles) lives in **`~/work/cluster`**. Containerization moves
   "how to run an engine" into precis so others can use it without our
   fleet.

## Consequences

- **Positive.** Adding a tool-pack is pure plugin work (kind + migration +
  `job_type` + Dockerfile/role), zero core churn. The interactive surface
  stays sub-second (mint job / content-addressed cache hit); heavy work is
  async and cached. One IR means the LLM reasons over routes uniformly
  regardless of engine.
- **Cost.** Each compute node rebuilds its images (duplicated build effort
  — acceptable at current fleet size; a registry is the escape hatch when
  it isn't). First-call latency for an uncached target is real (minutes+),
  handled by the job handle + `requested`→job blocking, not a synchronous
  wait.
- **Known gap (filed, gripe 160213).** The seam opens the *write/validation*
  path only; the read-time inverse rewrite (`_INVERSE_RELATIONS`) is still
  Python-dict-bound, so asymmetric plugin relations don't auto-mirror on
  `links_for`. Slice-1 relations must be symmetric; the fix is to source
  inverses from `relations.inverse_slug`.
- **Deferred.** All six build slices (route+AiZynth → LinChemIn normalize →
  ASKCOS → AlphaFold → sequence design → ChemCrow). ChemCrow lands as a
  planner coroutine that calls the verbs, not as a tool. nvidia-container-
  runtime stays unwired until a GPU engine needs containerizing.
