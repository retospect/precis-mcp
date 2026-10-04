---
status: draft
title: Pathway diagram draws one step per chemical change, annotated with what enters and leaves
pillar: 3d-design
prio: high
---

# Pathway diagram draws one step per chemical change, annotated with what enters and leaves

## Motivation / why

Reto, 2026-10-02, on pw455722 (`/refs/pathway/455722`): the grey dashed
sawtooth under the reaction path is confusing. It is the H-supply edges
(`kind: "supply"`, X → X+H, one H⁺+e⁻ from the reservoir under CHE),
drawn as separate dashed lines in their own x-columns
(`pathway_detail.html.j2`, the `e.kind === 'supply'` branch of the edge
loop). Under the parked template every supply drop is the same H
adsorption energy, so each hydrogenation reads as "dashed down, solid
up". The reader cannot see what entered or left at each step, so the
chain does not visibly balance: O stays behind after N–O dissociation,
H₂O and NH₃ leave, H arrives, and none of it is labelled. Item 3 of
`pathway-viewer-ux-batch.md` (where H₂O leaves) is the same complaint
from the desorption side; this item subsumes it.

## In scope

1. **Fold supply into the reaction step.** X → X+H (supply) → XH
   (reaction) draws as one solid hump X → XH in the path colour. The
   X+H level stays as a small shoulder tick at the hump's foot (its
   energy is real, and under the coadsorbed template it is a real
   state), not its own column. The x-layout counts chemical steps only.
2. **Step annotations with an arrow.** Each step carries what entered
   and left, above the hump: `+H⁺+e⁻ ↓` for a supply-fed hydrogenation;
   `−H₂O ↑`, `−NH₃ ↑`, `−NH₂OH ↑` for a desorption; `O*` (spectator,
   stays) where a dissociation leaves a fragment behind; `+NO ↓` where a
   second substrate co-adsorbs. Annotations follow U (the `+H⁺+e⁻`
   steps are the ones the slider moves) and fade with path selection like
   every other element.
3. **Engine contract (catpath).** Each graph link carries
   `added: {element: count}` and `removed: {species: count}` (catpath
   infers this in `ledger.py::link_fields`). Catpath retains `kind=supply`
   on links and emits `link_type` ∈ `adsorption | supply | desorption`;
   reaction edges retain `kind=reaction`. This absorbs
   `catpath-desorption-link-kind.md` (desorption is a real cost, not a
   ΔE = 0 supply convention) as its first slice.
4. **Fallback for old graphs.** Pathways without `added`/`removed`
   infer them from the state labels (fragment multiset difference), so
   every existing pathway renders the new way.

## Explicitly NOT in scope

- New chemistry or energies — this is presentation plus a link-field
  contract. Desorption energies become real in slice 3 only where
  catpath already computes them.
- The shared presentation module (`pathway-presentation-shared-module.md`,
  plugin-split) — if it lands first, this work goes into it; it does not
  start a second copy.
- Keyboard navigation and the other `pathway-viewer-ux-batch.md` items.

## Acceptance criteria

- pw455722 renders with no dashed supply segments: the NH₃ route reads
  NO@N → N+O → NH → NH₂ → NH₃ with `+H⁺+e⁻ ↓` on each hydrogenation and
  `O*` on the dissociation; the O route ends `−H₂O ↑`.
- Summing `added` and `removed` along any root→leaf path reproduces the
  leaf's stoichiometry from the root (a test over a coadsorbed and a
  parked fixture graph).
- Moving the U slider moves only the `+H⁺+e⁻` steps' levels, as today.
- A pathway with no `added`/`removed` fields renders the same
  annotations by label inference (fixture from a pre-change graph).
- Verified by a canvas/DOM check on the rendered SVG, not only a green
  suite.

## Target + blast radius

`src/precis_web/templates/refs/pathway_detail.html.j2` (diagram render,
x-layout, fork-probability guard which already excludes supply edges);
`src/precis/utils/reaction_graph.py` (`_is_supply`, route-step ΔG — the
folded step must keep the supply ΔG in the bottleneck read);
`src/precis/quest/figures.py` (filters supply edges); catpath
`graph.py` link emit + `network.py`. Needs a catpath version bump and
wheel redeploy (`catpath-wheel-version-reuse.md`).

## Open questions / decisions log

- **Bounded adapter prerequisite (coordinator, 2026-10-04):** pass
  `link_type` through `_pathway_graph_payload`; use it for explorer folding
  and annotations, falling back to `kind` on legacy graphs. Only a supply
  followed by a reaction folds; adsorption/desorption retain their columns.
  Molecular adsorption's `added.H` counts molecular hydrogen atoms, not
  protons from the reservoir. Preserve the kinetic fork guard's raw `kind`
  check: a typed link does not acquire a measured activation barrier.
  Synthetic tests must cover desorption→reaction, supply→desorption,
  adsorption→reaction, supply→reaction and legacy fallback, including the
  actual route payload and rendered SVG. No engine/pin update or experiment;
  release SHA selection and items 23/25 remain held. Version/lock integration
  stays with the coordinator's release change.
- Shoulder vs. no shoulder for the parked template, where X+H is
  bookkeeping only: default shows it (the energy is a real number either
  way); decide on Reto's look.
