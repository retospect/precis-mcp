# Native autocatpath integration — remaining slices

Design-of-record for reaction pathways as first-class structures. Most of
it SHIPPED — present-state lives in the `src/precis_pathway/` module
docstrings and ADR 0069; the full original design (the four tensions, the
code-grounded architecture, slice-0/1 build findings) is git history of
`docs/backlog/autocatpath-integration.md`. Shipped: the bundled in-tree
plugin (`pathway` kind, `pathway_body` chunk, plugin-namespace migration,
`PRECIS_AUTOCATPATH_ENABLED` dark gate), ssh_node routing to the pinned
GPU node, per-`(model, seed)` fan-out (`autocatpath_seed` /
`autocatpath_aggregate` job types), native structure ingest
(`ingest.py::scene_from_ase`, bond-free slabs, `related-to` links +
`meta` state-map), `KindSpec.can_own_jobs`, and the interactive explorer
(reaction-pathway-explorer, shipped 2026-08-06).

## Decided constraints that survive (keep honoring these)

- **autocatpath stays a pure engine** (numpy/scipy/ase/rdkit, zero precis
  dependency); precis imports it in-process — both GPL-3.0-or-later, same
  owner. autocatpath owns the science (network enumeration, NEB, pooled
  uncertainty); precis owns storage/provenance/serving.
- **§3.4 honest-number loop:** autocatpath pools `seeds: [0,1,2]` into
  `Estimate(mean±std, low_confidence)`; `low_confidence` must propagate to
  cite time — `citation.verifier_confidence` is the carrier, so a draft
  citing "barrier 0.8 ± 0.3 eV (low confidence)" inherits the flag and the
  reader badges it. A precis-wide `Estimate` primitive is flagged, not
  built (keep pathway-local until a second consumer forces it).
- **Config is authoritative; numbers/diagrams are derived** — regen is a
  content-addressed cache hit for unchanged intermediates.

## Remaining slices

- **Slice 2 — heavy backends.** Per-backend images/venvs (§3.5: MACE,
  CHGNet, FAIRChem, GRACE cannot co-install — `model → image/capability`
  table, GPU nodes advertise which they hold; MACE is the shipped
  default); cross-model `view='compare'` (a query over the
  per-`(model, seed)` partials); optional saddle/TS structure per edge.
- **Slice 3 residue — edit-by-prompt + regen button** on the `/pathway`
  web reader (the explorer's read surface shipped).
- **Slice 4 (optional) — unify relaxers.** Route autocatpath's individual
  relaxes through the `struct_runs` run-cube for per-state (not per-seed)
  caching; NEB band → `struct_frames` (that half is now owned by
  `pathway-frame-capture.md`). Only if the per-state cache economics
  justify it; pooling reuses `autocatpath.uncertainty.aggregate()` over
  run-cube rows, not a rewrite.
- **`Relation` core add:** a dedicated `pathway-node` link relation
  (ingest ships on symmetric `related-to`; the precise `{state → ref_id}`
  map lives in pathway `meta`, so nothing depends on link semantics).
  Same closed-registry issue as `JOB_PARENT_KINDS` was — consider a
  `can_own_jobs`-style plugin extension.

## Related

- `pathway-frame-capture.md` — retains NEB/relax geometries as
  `struct_frames` + cross-state atom identity (the motion half).
- `catalyst-physical-realism.md` — defect ensembles + poisoning-awareness
  over this pipeline.
- `cluster-scheduling.md` §B-1 — the per-seed chunking that fixed the GPU
  wedge (shipped).
