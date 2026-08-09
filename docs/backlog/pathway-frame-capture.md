---
status: draft
title: Pathway frame capture — retain NEB/relax geometries, store as struct_frames, establish cross-state atom identity
model: opus
---

# Pathway frame capture — retain NEB/relax geometries, store as struct_frames, establish cross-state atom identity

> The cross-repo data seam behind the *motion* half of
> `reaction-pathway-explorer.md` (which is precis_web-only and ships without
> this). Extends the `structure` kind's frame model (ADR 0043 §6.9 / §16.C) and
> the catpath pipeline (`docs/backlog/autocatpath-integration.md`). The
> pathway glue now lives in-tree at `src/precis_pathway/` (bundling shipped —
> ADR 0069); `gpu-priority.md` (re-chunks the job this hooks — see
> open questions). Motivating candidate: 175949 (Pd(111) NO→NH₃).

## Motivation / why

catpath *computes* the reaction motion and then throws it away. `neb.py`'s
`_neb_attempt` builds the climbing-image band of `Atoms`, runs it, and returns
`BarrierResult.images_energy` — a `list[float]` — dropping every image geometry;
`relax.py` runs BFGS with no `trajectory=` logger, keeping only the final frame.
So the transition-state geometry and the atoms' path over each barrier — the
most watchable thing a pathway produces — exist for milliseconds and vanish. To
let the explorer *animate* the cell over a barrier (and to track one physical
atom across states) we must retain those frames, store them where dense arrays
belong, and — the load-bearing part — give atoms a **stable identity across
states**, which does not exist today.

## Current state

- **Discarded motion:** `catpath/src/catpath/neb.py` (`BarrierResult.images_energy:
  list[float]`, image `Atoms` dropped), `catpath/src/catpath/relax.py` (BFGS, no
  trajectory). Confirmed by `/ready` review.
- **Frame home exists and is already in use — for scalars only:** ADR 0043
  §6.9 + §16.C specify the per-run frame table, live as `struct_frames`
  (`(id, run_id, step, energy, max_force, positions jsonb, created_at)`).
  It is **already populated today** by `structure_record_run`
  (`src/precis/store/_structure_ops.py`): one scalar row per relax step
  (`energy=NULL, max_force=<curve value>`, `positions` never written), read
  back by `structure_find_cached_run` as the convergence curve
  (`WHERE max_force IS NOT NULL ORDER BY step`). The §6.9 **geometry** tier
  (`positions`) has no writer, and the §16.C `positions_ref` blob-ref tier is
  designed but not yet a column. Capture must coexist with the scalar
  writer/reader contract — see Seam B.
- **No cross-state atom identity:** `catpath/src/catpath/precis/ingest.py::scene_from_ase`
  assigns atom labels (`aN1`, `aO2`, …) from a *per-state-local* element counter;
  identity state-to-state is only an implicit convention (the `StateSpec`
  fragment lists in `catpath/src/catpath/network.py` happening to enumerate
  adsorbate atoms in the same order).

## In scope

**Seam A — capture (catpath science engine).** `BarrierResult` carries per-image
atomic positions (not just energies); `relax()` gains an optional trajectory
logger. These are pure-engine files (`neb.py`, `relax.py`, `structures.py`) that
stay in the catpath repo under either code layout (see Cutover interaction).
Capture is **behind a per-run flag**, default off, so the live quest is
untouched until explicitly enabled.

**Seam B — store (reuse `struct_frames`, add the §16.C blob-ref tier).** The
glue (`persist/ingest/job`, today at `catpath/precis/`, post-bundle at
`src/precis_pathway/`) writes captured frames into **`struct_frames`**, not a new
table. Four reconciliations (the `/ready` reviews' demands, now decided):
- **Coexistence with the scalar-curve contract.** `structure_record_run`
  already writes one scalar row per relax step, and `structure_find_cached_run`
  reads the curve back via `max_force IS NOT NULL`. Capture **extends
  `structure_record_run`** with an optional per-step geometry payload rather
  than adding a second writer: a capture-enabled run's rows carry `positions`
  (or `positions_ref`) alongside the existing scalars, same `(run_id, step)`
  indexing; geometry-only NEB rows carry `energy` + `positions` with
  `max_force` NULL, so the cached-curve read is unaffected by construction.
  Row discriminator: geometry present ⇔ `positions IS NOT NULL OR
  positions_ref IS NOT NULL`.
- **`ref_id` fit for a two-terminus band.** `struct_runs.ref_id` is `NOT NULL`
  and single-valued; a NEB spans reactant + product refs plus images belonging
  to neither. Decision: the NEB run row **FKs the reactant-state ref** (the
  edge is directed reactant→product, matching the graph edge), `on_version` =
  that ref's version, and `params.neb = {product_ref, edge, n_images, seed,
  climb}` records the other terminus and provenance. The single-state scalar
  slots hold the band's most valuable frame — the **climbing/TS image**:
  `energy` = TS energy, `final_geometry` = TS geometry, `forces` = TS forces.
  Relax-trajectory runs are single-ref and need no such mapping.
- **Per-seed reality (gpu-priority is shipped, not a future race).** NEB
  already executes **per seed** (`quest/compute.py::dispatch_autocatpath` →
  `catpath pipeline.run_one_seed`). Decision: capture is **per-seed** — each
  seed's band/trajectory mints its own `struct_runs` row (seed in `params`,
  hence a distinct `cache_key`) and frames FK that row. At aggregate, the
  **selected** seed's run ids are recorded on the pathway
  `meta.frames = {edges: {edge_key: run_id}, states: {state: run_id}}` — the
  explorer reads only those; non-selected seeds' bands stay FK'd but are the
  first GC tier (see NAS retention).
- **Tiering.** Per the DB-vs-NAS decision (final states in DB; dense frames
  regenerable → NAS), ship the ADR 0043 §16.C **`positions_ref`** column via a
  forward core migration: small bands stay inline in `positions jsonb`; large
  trajectories write a content-addressed blob under the NAS and store its key in
  `positions_ref`. Regenerable via `struct_runs.cache_key`, so the
  occasionally-backed-up NAS is the correct tier and `pg_dump` stays lean.

**Seam C — serve (frame endpoint).** Decided shape: **`GET
/refs/structure/{ref_id}/frames/{run_id}`** in `src/precis_web/routes/refs.py`
(frames hang off a run, runs off a structure ref — the route validates
`run.ref_id == ref_id`), with a read helper in
`src/precis/store/_structure_ops.py`. The pathway page resolves edge/state →
`run_id` via `meta.frames` and calls this structure-scoped endpoint; no
pathway-scoped frames route. Response: inline `positions` straight from
`struct_frames`, or streams the `positions_ref` NAS blob via the
`podcast.py:audio` FileResponse + traversal-guard pattern. Cross-host path
resolution uses a **new sibling** of `corpus_layout.rebase_onto_local` (that
function is hardcoded to a `/papers/` pivot and returns `None` otherwise — it is
*not* reusable as-is; a `pathway-frames`-aware rebaser is required).

**Seam D — cross-state atom identity.** The enabler for same-atom-across-states
measures and continuous animation. Within one NEB the band shares atom ordering
by construction, and a band's endpoints *are* the two adjacent states — so
identity propagates along the chain: state → band(state→next) → next state.
Supply edges (barrierless H-from-reservoir) are known atom *insertions*, not
reorderings, so the chain stays walkable. Capture emits a per-pathway atom-index
map (state → canonical atom id) built by walking that chain, stored on the
pathway `meta`. This is the concrete "how" for the risk
`reaction-pathway-explorer.md` could only name.

## Explicitly NOT in scope

- **The explorer UI** — diagram, viewer wiring, per-state measures all live in
  `reaction-pathway-explorer.md`. This proposal ends at "frames + identity are
  stored and serveable."
- **New DFT/physics** — capture retains what the existing solver already computes.
- **Containerizing compute, or moving the science engine** — orthogonal
  (code-home decided: in-tree `src/precis_pathway/`, ADR 0069).
- **Backfilling historical pathways** — capture applies to new/re-run pathways;
  re-running 175949 first is an ops call.

## Acceptance criteria

1. A pathway run with capture enabled retains, per barrier edge, the **NEB image
   band** (positions per image + energy) and, per state, the **relax trajectory**;
   with capture off, behaviour is byte-identical to today (live-quest safety).
2. Captured frames persist in **`struct_frames`** FK'd to a `struct_runs` row —
   small bands inline in `positions`, large trajectories as a NAS blob keyed from
   the new `positions_ref` column; the frame endpoint serves both transparently.
3. Losing a NAS frame blob leaves the DB (graph, keyframes, `struct_runs` scalars,
   inline small frames) intact and the sequence regenerable from `cache_key`.
4. Capture emits a **cross-state atom-index map** on the pathway `meta`; for
   175949, a chosen adsorbate atom resolves to the same canonical id in every
   state it exists in (verifiable: the N of NO\* maps to the N of N+O\*).

## Target + blast radius

- **catpath (science, stays in repo):** `neb.py` (`BarrierResult` carries image
  coords), `relax.py` (trajectory logger), possibly `structures.py`.
- **Glue (`catpath/precis/{persist,ingest,job}.py` pre-bundle →
  `src/precis_pathway/` post-bundle):** emit frames + the atom-index map.
- **precis core:** forward migration adding `struct_frames.positions_ref`;
  `src/precis/store/_structure_ops.py` — extend `structure_record_run` with the
  optional per-step geometry payload + a frames read helper (coexisting with
  the scalar convergence-curve rows it already writes); a `pathway-frames` NAS
  rebaser sibling to `corpus_layout.rebase_onto_local`.
- **precis_web:** `GET /refs/structure/{ref_id}/frames/{run_id}` in
  `src/precis_web/routes/refs.py`.
- **NAS:** new `/botshome/pathway-frames/` subtree + a retention/GC policy.
- **Docs:** `docs/backlog/autocatpath-integration.md` (capture seam), ADR 0043
  cross-ref (§6.9 frames now populated), `state-map.md`.

## Open questions / decisions log

- **Frame blob format.** `.npz` (numpy-native, compact) vs `tar.zst` of extxyz
  (self-describing, ASE round-trips, matches catpath's existing extxyz wire form
  at `runner._structures_extxyz`). Lean: extxyz-in-`tar.zst`.
- **NAS retention / GC.** `/botshome` is occasionally backed up; frames are
  regenerable, so set a retention/GC policy (mirror `llm_blob` / `worker_logs`
  GC) + a "regenerate on miss" path rather than unbounded growth. Cap or TTL —
  open.
- **Cutover interaction — RESOLVED**: bundling shipped (ADR 0069); the glue
  edits target `src/precis_pathway/`. Science edits (`neb.py`/`relax.py`)
  stay in the catpath repo.
- **Per-seed capture — DECIDED** (resolves the `/ready` blocker that treated
  shipped `gpu-priority.md` as a future race): NEB runs per seed today, so
  capture is per-seed; each seed's band mints its own `struct_runs` row and
  the aggregate records the selected seed's run ids on `meta.frames`. Losing
  seeds' frames are the first GC tier. (Full mechanics in Seam B.)
- **"Regenerable" is a property, not a tool — DECIDED** (resolves the `/ready`
  advisory on AC3): no regeneration CLI ships here. AC3 is demonstrated by
  re-running the same capture-enabled computation — identical inputs yield the
  same `cache_key`, re-minting the frames. A "regenerate on miss" convenience
  is future work under NAS retention/GC.
- **Broken identity chain: emit-time behavior — DECIDED** (resolves the
  `/ready` advisory): if the Seam D chain walk fails (disconnected pair,
  unexplained reordering), capture emits `meta.atom_identity = {status:
  'unavailable', reason: …}` — an explicit marker, never a partial or guessed
  map — and the explorer renders same-atom features disabled with that reason.
  RMSD-nearest matching stays a *future fallback*, not silently substituted.
- **Seam D split signal — acknowledged, staying together.** The `/ready` pass
  noted A–C (capture/store/serve plumbing) are independently valuable without
  D (identity algorithm). D stays in because the Motivation's same-atom
  tracking is load-bearing; if D slips during the build, it splits out as its
  own proposal `blocked-by` this one's A–C remainder.

*`/ready` pass (2026-08-06): 4 blockers (struct_frames-already-populated,
`ref_id` fit for a two-terminus band, per-seed reality, endpoint "or") resolved
into the Seam B/C decisions above + corrected Current state; 3 advisories
resolved into the three DECIDED entries above. Genuinely open: blob format
(lean recorded), NAS retention/GC, bundling sequencing note.*
