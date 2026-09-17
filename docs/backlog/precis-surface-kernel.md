---
status: draft
title: precis_surface — lattice-agnostic smooth-surface kernel alongside se, with hexfold as its carbon binding
prio: high
model: opus
blocked-by: hexfold-integration
---

# precis_surface

Design 2026-09-16 (Reto + agent). **The design lives in
`src/hexfold/spec.md` Part III** (§20 smooth layer, §21 `smooth:`
grammar, §22 budget/placement, §23 editing model, §24 se interchange,
§25 MCP surface, §26 catalogue, §27 self-intersection). This item is the
build plan and the precis-side decisions only.

## Package split (decided)

- `src/precis_surface/` — lattice-agnostic: patches, rims as Dirichlet
  curves, seams as Plateau film clusters, curvature bound in caller
  units, direction fields with prescribed singularities, embedding
  checks, mesh realisation. Metres, se frame conventions. Useful for any
  surface-shaped thing (membranes, the 30 nm oval box tiling), not only
  carbon.
- `hexfold.smooth` — the carbon binding: hex-lattice singularity charges,
  σ, the discretiser that emits the discrete `.hx` section from a solved
  surface, the `smooth:` parser/emitter. `precis_surface → hexfold`;
  hexfold never imports precis.
- Storage: a `surface` **binding kind** on se blocks (alongside
  `cad | structure | component | part`), not a new precis kind (no
  totality pincer, no migration for a kind). Rim frame = se datum
  (`se-datum-measure-eval`); cross-scale addressing =
  `se-pick-hierarchy`.
- Cache: `CatalogueStore` protocol (`get`/`put`), DB first — table
  `hexfold_cache(key, format_version, generator, fidelity, authored_json,
  generated_json, created_at)`, one migration; se block `topology`
  references the key. File backend arrives with the pip re-export.
- MCP: reuse `generate` (with `fidelity`), `add_port`, `connect`,
  `set_binding`, views; new small `view='surface'`, `view='catalogue'`,
  op `move_handle`; the one new handler is `options(handle, wish)` =
  `fit.alternatives` surfaced as ranked candidates.

## Build order (spec §28.4–6; each swap local, chain solver never changes)

1. [ ] **Stage 1 — symbolic chain solver with a stub geometry backend.**
   Parts report rim indices and a rough length; the whole chain solves
   both directions from pinned ends. This alone proves the interface
   claim (an LLM driving the system end to end) before any geometry.
2. [ ] Straight tubes; symmetric collars; caps from the cache.
3. [ ] Discrete-mesh smooth solve (area minimisation, Pinkall–Polthier;
   film clusters à la Surface Evolver), the two-part curvature bound
   (`smooth.bend`, `smooth.singularity_spacing`), seams with the 120°
   condition. Closed forms (catenoid, Schwarz P/D patches, C60 tables)
   as seeds and test oracle only.
4. [ ] Direction field (N-RoSy with prescribed singularities) →
   commensurability before discretising; then the bent collar (search
   → fitting) behind the `fit` family interface.
5. [ ] Self-intersection: BVH in the loop, CCD during relaxation,
   repulsive energy on finalists (implemented from the papers, not the
   reference source).
6. [ ] `strain_max` default from a literature lookup (spec §29 Q5).

## Open (spec §29, verify early)

Q1 bent-collar twist well-defined on the rim loop; Q2 field ≡ ring
placement holds for charges (Poincaré–Hopf), positions are hex-remeshing
integrability; Q3 bent-collar search — instrument before trusting
interactivity.
