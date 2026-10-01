---
status: draft
title: every structure says which tier made its current coordinates (stick, spring cleanup, force field, ML, DFT), and the 3D viewer shows it
pillar: 3d-design
---

# Every structure says which tier made its current coordinates

Reto, 2026-10-01: "the structures state of integration (quick spring, fully
relaxed, ml potential, dft) should be visible to the viewer - how best?"

## Motivation / why

The same atoms mean very different things depending on what placed them,
and today nothing on screen says which. The hexfold nanobud drums
(se `hexa-nanobud-drum`, the rounded variant) were measured three ways on
2026-10-01, and the numbers only make sense with the tier attached:

- hexfold stick (a spring preview, not physics): C–C 1.42 ± 0.005 Å, by
  construction, because the springs pull every bond to σ.
- the `geo` rung on prod is the same class of spring cleanup.
- a Tersoff force-field relax of C60 moves its bonds to 1.46–1.50 Å; the
  pyramidalization stays at 11.6°.

A viewer that shows a pretty drum without its tier invites reading a spring
preview as a relaxed structure. gr459602 (bond and strain statistics) has
the same need: a θp of 13° from stick and one from DFT are different claims.

What exists: `struct_runs.fidelity` records each relax run, and the
structure web page (`structure/detail.html.j2`) lists runs and a revision's
`energy_fidelity`. What is missing:

1. **The tier of the coordinates themselves.** A revision minted by a
   generator (the se hexfold generator's `build` → `stick`) has no run, so
   its tier is implicit. An edit after a relax silently makes the last run's
   label stale.
2. **The se 3D viewer** (`blocktree-3d.js` atomic overlay) shows no tier.

## In scope

- One per-revision field, `geometry_tier`, from an ordered vocabulary:
  `seed` (generator placement, hexfold stick, spectral embed) < `clean`/`geo`
  (spring cleanup) < `emt`/`ff` (classical potential) < `xtb` < `ml` <
  `dft-fast` < `dft-tight`. It is written by whatever wrote the coordinates:
  a generator writes `seed`, and a relax run writes its rung on its output
  revision. Any coordinate-changing edit op resets it to `seed` unless the op
  is itself a relax, plus `stale_since: <op>` naming the edit.
- Per-region tier when a relax was pinned or partial (the hexfold join seam
  re-relax, a `pinned` geo relax): store the movable mask's atom count, so
  the label can say "geo, 312 of 2832 atoms".
- The viewer: a tier badge in the corner of the 3D pane, coloured on the
  ladder (grey `seed`, amber spring, green potential/ML, blue DFT), with the
  stale marker. The badge's tooltip carries the run id, model, and converged
  or not (fmax, steps). This is the same data as the web page's run list,
  promoted onto the canvas.
- `get(kind='structure', view='validate')`, gr459602's stats view, and the
  `hexfold check` geom findings print the tier alongside every number.

## Explicitly NOT in scope

- Running higher tiers automatically. This item only labels what ran.
- Per-atom colouring by tier. Region counts are enough until someone asks.
- Changing which rungs exist (`relax.py`'s ladder is untouched).

## Acceptance criteria

- A fresh se hexfold build's structure reads `geometry_tier=seed` over MCP
  and in the viewer badge.
- After `edit(ops=[{op:relax, fidelity:geo}])` it reads `geo`, with the run
  id and convergence (the 200-step, unconverged pillar relax shows
  "not converged").
- A subsequent atom-moving edit shows `stale_since` in both places.
- gr459602's stats output names the tier.

## Target + blast radius

`precis.structure` (store revision row; migration: new nullable column,
forward-only), `relax.py` (writes the tier on its output revision), edit ops
that move atoms, the se hexfold generator, `precis_se` atomic3d.json export,
`blocktree-3d.js` overlay, `structure/detail.html.j2`, the validate view.

## Open questions / decisions log

- Store on the revision row, or derive from the last `struct_runs` row plus
  an edit-since check? Deriving needs no migration, but it can't label
  generator output. A column is proposed.
- Does `emt` sit with `ff` or below it? EMT's carbon parameters are poor, so
  for carbon it is closer to a spring cleanup.
