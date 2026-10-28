---
status: draft
title: "Design re-runs pull from prior successful runs — a run archive, content-keyed replay and elite warm starts"
pillar: 3d-design
prio: normal
---

# Design re-runs pull from prior runs

Origin: gr512900 (cluster `design-rerun-reuse`, `size:l`, `fix:spec`).
Reto 2026-10-24 (chat-99): "can we persist the state so prior, if
successful, provides some pull? Such that if I re-run a PCB that has been
stored and recovered, without changes, I get an identical or near
identical one back. The prior encodes relevant history: if the components
are placed similarly, the routes could go similarly; a set of component
placements that led to successful routing is a good set to cluster
around." And: "all prior knowledge on this thing from all relevant runs
should kinda persist in a statistical way … it should be clever enough
to kinda tolerate code and data structure changes." Perplexity research:
`perplexity-research:methods-for-persisting-statistical-prior-knowledge-across-re`
(unverified secondary source; gripe comment 2 maps it onto our code).

## Problem

A re-run learns nothing from the runs before it. Once a `pcb_place` job
goes terminal, resubmitting the same op on the same design mints a fresh
job that anneals from the constructive seed again — the idempotency key
deliberately stops matching terminal jobs
(`JobHandler._lookup_idem`). The only durable trace of a good run is the
design's current poses plus a single overwritten `refs.meta` slot
(`meta.last_place`); the topo placer's per-run elite archive lands in
`meta.topo_run.files['elites.json']` and is never read back. Evidence:

- gr512900: no general archive of successful placement/routing states; a
  stored-and-recovered board re-runs cold.
- The workspace direction already assumes this cache:
  [pcb-workspace-code](pcb-workspace-code.md) §3 keys a script's
  place/route request by the board's content hash and lets the importer
  "dispatch the existing job types **or answer from cache**" — the cache
  is this item.
- Same shape at two other scales: `struct_search` keeps its AGOX
  database as a per-job artefact (`StructureSearchResult.database_path`)
  with warm-start specced but unbuilt
  ([global-structure-search-slices](global-structure-search-slices.md)
  slice 2); `hexfold_relax` checkpoints resume the *same* job only
  (`src/precis_se/hexfold_relax_job.py::_resume_state`).

## Current behaviour

Anchors are symbols, not lines.

- A design is a `refs` row (`kind='pcb'` here; `kind='se'`,
  `kind='structure'` elsewhere). A run is a `kind='job'` ref with
  `parent_id` → the design ref, params and `meta.idem_key` on the job
  (`precis.handlers.job.JobHandler.put`), outcome as a `STATUS:` tag plus
  `job_summary` chunks.
- `PcbHandler._enqueue_op` digests the authored design +
  params via `precis.pcb.session.content_hash` →
  `idem_key = pcb_{op}:{ref.id}:{digest}`. `content_hash` covers exactly
  the surfaces a run reads (instances, nets, features, route topology
  overrides, planes, measures, fixed copper) and deliberately excludes
  every job-written field, so it is already the "semantic design hash"
  the research's layer 1 asks for. But `_lookup_idem` matches only
  **non-terminal** jobs: a succeeded run never satisfies a resubmit.
- Placement: `pcb_place._dispatch` → `optimize(ir, OptimizeConfig)` — the
  SA anneal seeded by `seed_placement` (connectivity clusters, shelf
  packing; `params.seed`, default 0) — or `placer='topo'` →
  `topoplace.topo_place`. Results commit through `pcb_set_pose` behind a
  never-worse pre-route DRC gate; `meta.last_place` on the design ref
  records `{iters, cost_before, cost_after}` only — no poses, no elite
  set, one slot.
- The topo placer already keeps a diverse in-run `EliteArchive`
  (`topoplace.archive`: admit only if ≥ `d_min` displacement or ≥
  `rot_frac` rotations differ) and serializes it with the run manifest
  (`topoplace.runstore`: `RUN_SCHEMA`, `CODE_VERSION`, `board_hash`,
  per-part footprint shas, `elites.json`/`pairs.json`/`pins.json`) into
  `meta.topo_run.files` — written for `view='topo'`, never consumed as
  input.
- Routing: **retired 2026-10-25** (`precis.pcb` docstring,
  `ROUTE_RETIRED`) — the vector router is deleted, the topo realiser is
  unlanded. `meta.last_route.warm_start` / `format_warm_start` survive
  only as records on boards routed before the cut (supply-role pin
  matching — narrow).
- Design versioning exists but does not record runs:
  `design_checkpoints`/`design_branches`/`design_revisions`
  (`precis.design.history`; migrations 0162, 0169), the pcb revision md5
  `_PCB_REVISION_SQL`. A branch/checkpoint snapshots *design state*;
  nothing snapshots *a run's outcome* keyed to the input that made it.
- `hexfold_relax_job` (se): per-step node-local checkpoint file +
  `meta.relax_checkpoint` pointer, resumed on reclaim of the same job —
  suspend/resume, not re-run reuse.
- Determinism: the anneal reproduces given identical input + seed, but
  nothing is *pinned* — the research's caveat applies (thread order,
  dependency drift, GPU atomics break seed-only identity).

## Proposed design

Four layers, lifted from the research's mapping and re-scoped to what
the code already half-has. Layer order is also the slice order.

**D1 — run records (the event store, thin).** Every terminal `pcb_place`
run records, against `(ref_id, op, content_hash)`: params + seed,
`CODE_VERSION`/`RUN_SCHEMA`-style version tags, the resulting pose map
(`refdes → {x, y, rot, side}`), the elite set, objective scores, and the
outcome — committed, or refused with the gate verdict. Refused runs are
recorded too (they are evidence of what did *not* work) but never serve
as replayable results. Carrier is an open question below — the terminal
job ref itself already holds `parent_id` + `idem_key` + `meta`, so "the
archive" may be a query convention over job refs rather than a new
table.

**D2 — replay on an exact hit.** When a resubmit's `content_hash` equals
a *terminal succeeded* run's key, serve the recorded result instead of
minting a job — either as a direct answer (the workspace importer's
cache path) or as a no-op job whose summary names the source run.
Identity comes from replaying stored output, never from re-executing and
hoping the seed reproduces (the research's literal caveat). A record
whose schema/version the current code cannot read is skipped with a
logged reason — code changes cost the cache, never the run.

**D3 — elite warm start on a changed input.** On a hash miss, the
placer's start set becomes: the constructive `seed_placement` seed **+
the top-K stored elites of this design's family**, matched by `refdes`
(parts added since an elite seed constructively; departed refdes drop;
`side` rides the stored flag). For the anneal this is multi-start — each
elite is an incumbent the SA walks from — or a soft anchor term
λ·‖pose − elite‖² inside the objective, decaying as current evaluations
contradict it (the research's trust-region shape). For topo, preload
`EliteArchive` from `elites.json`. Which pull — multi-start, anchor
term, or both — is an open question; the research warns a single
historic optimum traps the basin, so K stays > 1 either way.

**D4 — one seam, three renters.** The reusable abstraction is
`(design ref, op, content key) → recorded result + elites`. se relax
keys on structure + calculator settings (per the research); the result
is the relaxed geometry its checkpoint file already serializes.
`struct_search` formalises slice 2's `warm_start=<handle>` onto the same
record shape. None of D4 builds in slices 1–2; the pcb slices fix the
record's shape first.

**Not a layer yet — the statistical prior.** Per-task surrogates /
RGPE-style transfer across runs (the gripe's "Bayesian prior … tolerate
schema changes") is explicitly deferred: the research itself flags
unverified claims, negative transfer when unlike tasks pool, and
version-brittle pickled models. The event store of D1 is designed so a
later surrogate refits from raw records rather than migrating pickles —
that is the tolerance story, not a shipped model.

## Slices

Draft ranking; each slice lands alone.

**S2 — elite warm start + the cold/warm bench.** Top-K elite seeding by
refdes for the anneal; `EliteArchive` preload for topo. Measure on the
standing dogfood boards (`heater-base-test`, `taz15`) with a small ECO
(move/add 1–3 parts): final cost at equal iters, iters-to-cold-final,
and a displacement count over unchanged parts — gripe comment 3's
metric, minus the routing leg until S3.

**S3 — the route leg when a realiser exists.** Extend warm start from
poses to routed geometry (whole-route matching by net — the successor of
`meta.last_route.warm_start`'s supply-role pin matching). Blocked on the
topo realiser in [pcb-topo-place](pcb-topo-place.md); an elite only
counts once it routed (comment 3: "not finished properly placing until
it is routable").

**S4 — the seam generalised.** se relax records keyed on
structure + settings; `struct_search` warm-start rebased onto the shared
record (absorbing slice 2's node-local-path hazard ruling).

**Open item — workspace importer lookup.** S1 built the record and the
`_enqueue_op` replay door but NOT the importer side: when
[pcb-workspace-code](pcb-workspace-code.md) lands, its §3 request-cache
door consults the run record (`run_record.lookup`/`apply_record`) the
same way `_enqueue_op` does. There is no pcb workspace importer yet —
the seam exists, the second consumer does not.

## Explicitly NOT in scope

- Surrogates, Bayesian transfer, RGPE/multi-task ensembles — deferred by
  design above.
- Route warm start before the topo realiser lands; any new router work.
- Mid-run suspend/resume of `pcb_place` (the anneal emits no checkpoint
  events; hexfold's per-step machinery is a different job's slice —
  [relax-slices](relax-slices.md)).
- Cross-design transfer between unrelated boards (a shared global
  archive) — pending the family-scope ruling below.
- Changing `content_hash`'s coverage or the in-flight idem contract.

## Acceptance criteria

Draft — to be hardened at `ready`.

- A resubmitted `op='place'` against an unchanged design returns the
  stored placement with no optimiser call; the response names the source
  run.
- A changed design warm-starts from ≥1 stored elite matched by refdes;
  on the bench, warm reaches cold-final cost in fewer iterations, and
  unchanged parts move measurably less.
- Replay is bit-identical by construction (stored poses), not by
  re-execution luck.
- A record with an unknown schema/version is skipped with a logged
  reason; a refused-run record never serves as a success.
- In-flight idempotency (`_lookup_idem` over non-terminal jobs) is
  unchanged.

## Target + blast radius

`precis.handlers.pcb.PcbHandler._enqueue_op`,
`precis.workers.job_types.pcb_place._dispatch`,
`precis.pcb.session.content_hash` (read, not change),
`precis.pcb.optimize` (multi-start entry),
`precis.pcb.topoplace.archive`/`runstore` (archive load path), the job
record carrier (job meta vs new table — migration if a table), and the
workspace importer's cache hook in pcb-workspace-code §3. S3/S4 add the
route realiser and `precis_se.hexfold_relax_job` /
`precis.structure.search` surfaces. No changes to `pcb_set_pose`, the
DRC gate, or idem semantics for non-terminal jobs.

## Open questions / decisions log

All Reto's — none resolved, so this stays `draft`.

- **Unit of success, post-retirement.** Comment 3 made the stored unit a
  placement+route pair; the router was deleted the next day. Is a
  gate-clean committed placement the v1 elite (with S3 promoting the
  record to a pair when the topo realiser lands), or does the whole item
  wait on S3?
- **Replay vs re-anneal on an exact hit.** Serve the stored result
  verbatim, or re-anneal with the stored pose as incumbent seed (slower,
  self-heals under code drift)? "Identical or near identical" admits
  both.
- **Carrier.** A query convention over terminal `kind='job'` refs
  (parent + `meta.idem_key` + payload in `meta`, zero DDL) versus a
  dedicated `design_runs` table (one migration, cleaner indexing, fits
  the design-core naming). Jobs also carry retention/GC questions a
  table would not.
- **Family scope.** "All relevant runs" = this design and its forks, or
  a cross-design pool? The research warns pooling unlike tasks gives
  negative transfer; per-design-family is the safe default.
- **Staleness.** Does a record expire by version (unknown
  schema/`CODE_VERSION` → skip), by count (keep top-K per design), or by
  the design's own revision (`_PCB_REVISION_SQL` distance)?
- **Suspend/resume for pcb runs.** Native+portable double checkpoints
  (research layer 3) exist for hexfold only. Is mid-anneal
  suspend/resume wanted for `pcb_place`, or is its per-claim budget
  small enough that whole-run replay suffices?
- **Spec rot in the gripe.** It names `docs/backlog/pcb-global-plan-
  router.md`, which no longer exists — presumed folded into
  [pcb-topo-place](pcb-topo-place.md). Confirm.
