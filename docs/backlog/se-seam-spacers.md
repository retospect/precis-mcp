---
status: draft
title: Insulating seams — literature survey + demo that (CH2)n/C2H4 spacers make seam-local relax safe
pillar: 3d-design
prio: normal
model: opus
---

# Insulating seams: literature survey + demo

Reto, 2026-10-23: "We have insulating seams (like c2h4 spacers for dft) so
local chunks remain ok (for the backlog — lit search and demo to see these
spacers are ok!)". Owner thread:
[structure-workspaces](threads/structure-workspaces.md) (horizon).
Consumers: [structure-workspaces](structure-workspaces.md) §Relax
hierarchy (c) (seams, buffer radius) and
[se-flow-kernel](se-flow-kernel.md) slice 4b §Build-step stack cache
(seam-local relax above a `join`).

## Motivation / why

The build-step stack cache treats a post-join relax as seam-local: free
a buffer around the declared seams, freeze the far atoms, check residual
forces. That is cheap only if a seam exists that decouples the two
chunks — otherwise every join reruns a full relax and the assembly DAG
buys nothing at Å. Saturated linkers (C2H4, (CH2)n) are the assumed
insulators. The assumption is unverified for our chunks and our rungs
(xtb, DFT), and electronic insulation says nothing about mechanical
insulation: mismatch strain crosses a spacer that decouples electrons.
This item turns the assumption into numbers: which spacer, how many CH2,
what buffer radius, and when the force check fires anyway.

## In scope

**Slice 0 — literature survey.** Run through the session MCP's
`perplexity-research` kind on prod once built (`put(kind=
'perplexity-research', ...)`); cite the resulting `perplexity-research:
<id>` ref(s) here. Questions, each answered with sources:

- Fragment methods and what they assume at the cut: FMO, ONIOM,
  subsystem/embedding DFT, link-atom (H-cap) capping — where the cut may
  be placed, the error per cut, how the buffer region is chosen.
- Decay of electronic coupling through saturated σ linkers: the
  attenuation factor β per CH2 (tunnelling through alkane bridges;
  superexchange), and how it compares with π/conjugated linkers.
- Mechanical/strain transmission through linkers: how far lattice or
  geometry mismatch strain propagates past a flexible saturated spacer,
  and what makes a spacer stiff enough to carry it.

Output: a short table (method · cut rule · buffer rule · error estimate ·
source) and a one-paragraph β summary with the cited range, written into
this file.

**Slice 1 — demo.** Two chunks joined by (CH2)n, n = 1..4, plus a C2H4
spacer as Reto named it; each computed at `xtb` (GFN2, the existing
`precis.structure.relax.relax` rung) and at one DFT level (GPAW on
`PRECIS_DFT_NODE`, the `dft-fast` rung). For each n compare local
observables on each chunk against the same chunk isolated and H-capped:
partial charges, bond lengths in the chunk, forces on chunk atoms after
the joined relax, and HOMO/LUMO localisation (fraction of the frontier
orbital on each chunk). Find the n at which every delta drops below its
threshold (charges 0.02 e, bond lengths 0.01 Å, forces 0.1 eV/Å — the
`relax.residual_fmax` of §Relax hierarchy (c) — and frontier
localisation > 0.9 on one chunk). A strained case — the two chunks with
a deliberate lattice-period mismatch across the spacer — shows the
mechanical caveat: the electronic deltas pass while the force delta on
the far chunk does not, and the seam-local relax's buffer-growth loop
catches it.

Chunks: two hexfold units from the library design (`precis_se/library.py`;
a `cccR` prism and a k3 seam fragment) so the demo uses the chunks the
assembly DAG will join. The demo runs as a `struct_relax` job
(`struct_runs` cache), not in a sandbox.

## Explicitly NOT in scope

- Building the seam-local relax itself (structure-workspaces slice 4a)
  or the `join` step (se-flow-kernel slice 4b); this item supplies their
  numbers.
- Conjugated or metallic linkers; a second DFT functional; k-point
  convergence beyond the service's default rule.
- A general fragment-method implementation (FMO/ONIOM as a service).
  The survey names them; the seam-local relax stays our own fixed-mask
  relax with the force check.

## Acceptance criteria

- Slice 0: the table and β summary above are in this file with
  `perplexity-research:<id>` refs for every row.
- Slice 1: one table per n (and the C2H4 row) with the four deltas at
  both rungs, plus the strained-case row; a recommended default spacer
  and buffer radius for seam-local relax written into
  structure-workspaces §Relax hierarchy (c) in the same commit, with the
  threshold each number clears; the strained case shows a force delta
  above threshold with the electronic deltas below it.
- The demo structures and runs are `struct_runs` rows reachable from
  this file's refs; no numbers live only in a chat.

## Target + blast radius

Docs and numbers only until slice 1 runs: `precis.structure.relax.relax`
(existing rungs, no change), `struct_relax` jobs on `PRECIS_DFT_NODE`,
the library design's units. Changes the defaults the seam-local relax
reads (`relax.scope: 'interfaces'` shell radius, `residual_fmax`) — the
blast radius is every post-join relax once slice 4a of
structure-workspaces lands.

## Open questions / decisions log

- 2026-10-23: filed on Reto's direction (verbatim above). Thresholds in
  slice 1 are proposals, to be set by the survey's error estimates.
- Which DFT level — `dft-fast` as the service's default; a tighter level
  only if the xtb/DFT disagreement at the chosen n is above threshold.
