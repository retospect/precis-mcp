---
status: draft
title: se viewer strain layers — bond length on bonds, angle strain on atoms, each with a threshold slider
pillar: 3d-design
prio: high
---

# se viewer strain layers — bond length on bonds, angle strain on atoms, each with a threshold slider

## Motivation / why

Reto, 2026-10-02, looking at se `hexa-smooth-drum-v2`: next to the
atomic ↔ smooth slider he wants angular bond strain and bond-length
deviation as well, in different colours, all at the same time. The use is
finding the weak points of a carbon structure, for example the "organic"
stem of a smooth drum (backlog/smooth-drum-engineered-mode.md).

Three scales blended on one surface cannot be read: one pixel cannot show
three numbers. So each measure goes on a different part of the picture
with its own colour family and legend (agreed with Reto 2026-10-02):

| Layer | Drawn on | Measure | Colour family |
|---|---|---|---|
| surface deviation (exists, gr450675) | smoothed surface | distance to the Taubin-smoothed sheet | today's pale-blue → blue → dark-red |
| bond length | bond cylinders | \|l − l₀\|, l₀ = 1.42 Å for C–C | green |
| angle strain | atom spheres | pyramidalization θp (POAV, `precis_surface.relax.theta_p_deg`) | orange |

Angle strain is θp (Reto, 2026-10-02): it is what makes pentagons and
heptagons stand out. The in-plane deviation from 120° is not used.

## In scope

- Server: `atomic3d.json` (`_atomic_block_payload`,
  `src/precis_web/routes/blocktree_view.py`) gains per-bond `bond_dev` and
  per-atom `angle_strain` arrays, next to the existing `deviation`.
  Computed for sp² carbon; a block where a measure does not apply omits
  it, and its control stays hidden (the target overlay's convention).
- Viewer (`static/blocktree-3d.js`, `templates/blocktree/detail3d.html.j2`):
  next to the atomic ↔ smooth slider, one control row per layer: a
  checkbox, a **threshold slider**, and a legend with min/max in Å or
  degrees.
  - Below the threshold, elements stay their normal grey; at or above it
    they take the layer's colour scaled from threshold to max. On a
    6000-atom drum this leaves only the hot spots coloured.
  - **Default threshold: the 95th percentile** of the measure on the
    loaded structure (Reto: "auto-set them to maybe 5%" = the top 5% of
    elements are coloured at load). The slider runs from the measure's
    min to max; the legend shows the current threshold value.
  - All three layers can be on at once.
- The surface-deviation layer gets the same threshold slider, default 95th
  percentile, so the three behave alike.

## Explicitly NOT in scope

- Mixing two measures into one colour channel.
- Tersoff or any force-field energy per atom; these are geometric measures.
- Non-carbon reference bond lengths beyond a C–C default (a per-pair table
  is a follow-up when a non-carbon atomic block needs it).

## Acceptance criteria

- On se `hexa-smooth-drum-v2`, each layer's checkbox changes the canvas,
  and moving its threshold slider changes it again (pixel-diff in
  `scripts/viewer_check.py`, the se-3d-viewer thread rule: a green suite
  is not evidence).
- At load, with a layer on, the coloured element count is 5% ± 1% of that
  layer's elements.
- With angle strain on and the threshold at its default, the atoms of
  all 12 pentagons on that drum are coloured.
- Near the smooth end of the atomic ↔ smooth slider, atoms and bonds
  fade out, so the bond and atom layers only show toward the atomic end.
  The item states this; its legends grey out with the fade.
- `tests/test_web_se_atomic3d.py` pins the new payload arrays (length,
  C60's θp ≈ 11.6°, bond_dev 0 for an ideal 1.42 Å bond).

## Target + blast radius

`/se/{slug}/atomic3d.json` and the se 3D detail page only; payload grows by
one float per bond and one per atom. No store or handler change.
