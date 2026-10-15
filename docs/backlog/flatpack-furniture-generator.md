---
status: ready
title: Flat-pack furniture generator on a shared sheet job — the cardboard cut and the later exporters (builds 1–4 shipped)
pillar: 3d-design
prio: normal
model: opus
---

# Flat-pack furniture generator on a shared sheet job

Reto, 2026-10-03: "make a basic box with shelves from plywood of some
thickness, and make the pattern fit on a sheet to be laser cut or cut with
a Maslow CNC machine." The se-machine-design thread owns this item. Reto
ruled on it the same day:
- **se-machine-design-4:**
  - laser first; bed size is no constraint;
  - first material 3 mm corrugated cardboard;
  - finger joints stay the default, with a straight (plain butt) edge as a
    second option;
  - living hinges are filed separately (`flatpack-living-hinges.md`).
- **se-machine-design-5:** the first box is a 50 mm cube. There is no
  caliper reading; record the `fit` and `kerf` that seat on the first cut.
- **se-machine-design-7, option 1:**
  - a shared **sheet job** sits under the generator from build 1;
  - the laser, a CNC and the Cricut are all exported from it;
  - PCB writes into it through an adapter ewod-pcb owns, and never reads
    from it.

## What shipped (builds 1–4)

`src/precis/sheet/` (model, validation, laser SVG, Cricut SVG and DXF R12
exporters)
and `src/precis_se/flatpack/` (materials, panel model with finger/straight
joints, shelves, `fit`, dog-bones, mitre kerf, skyline nesting,
`flatpack_box(...)` emitting the sheet job). The geometry rules and the
exporter contract live in those packages' docstrings; the acceptance
figures (the panel table, 5 × 10 mm fingers, the kerf area formulas, the
10 000 mm² shortfall) are pinned in `tests/test_flatpack_panels.py`,
`tests/test_flatpack_nest.py` and `tests/test_sheet_job.py`; the 50 mm
cube's SVG is the golden `tests/fixtures/flatpack/cube50_laser.svg`.

The agent surface shipped in build 3: the `flatpack_box` op
(handler-level beside `realize`; one cad design + placed block + rigid
connect per panel under `<design>-<panel>` slugs, idempotent re-runs with
retired-panel cleanup in `precis_se/flatpack/se_op.py` and `mating.py`),
the `meta['flatpack']` params home (`put` merges it, `edit` writes it with
`meta || jsonb`), `view='cut'` (`format` `svg`/`dxf`, the notes in the
exporters' leading comment), and the store-aware `flatpack_*` checks in
`precis_se/flatpack/checks.py` appended by `_render_drc`. Skills
`precis-se-help` and `precis-se-flatpack-help` describe the surface; the
acceptance figures are pinned in `tests/test_flatpack_se.py`.

Build 4 added the Cricut export: `src/precis/sheet/svg_cricut.py` — the
same SVG restricted to `cut` and `score` (every other op refused by
name), with `data-pressure`/`data-blade` per layer from the layer's
`cricut` settings over `CRICUT_DEFAULTS`, wired as `view='cut'`
`format='cricut'`.

## The first physical check — Reto's

Not agent-gradable; handed over as a review-queue item once build 3 lands.

- Reto cuts the 50 mm finger-joint cube in 3 mm corrugated cardboard on
  his laser.
- The `fit` and `kerf` that seat are recorded as `corrugated-3mm`'s laser
  figures in `materials.py` and the generator's docstring, in a follow-up
  commit.
- If the first cut does not seat, the record says what changed, and the
  figures that seated are the ones recorded.

## Builds

Later, each its own build or less:
- **Grayscale raster engrave.** Image placement (the image, a placement
  rectangle, the dpi), the grey-to-power map over the laser layer's power
  range, and embedding in the laser SVG. One build, when Reto wants images
  on panels; the model already reserves `engrave_raster` and every
  exporter refuses it by name.
- **The neutral move of the `pcb/geom.py` helpers and the stroke font** into
  `precis/sheet/`, with shims at the old paths. Half a build, owned by this
  thread, and sequenced with the parked pcb-easyeda-round-trip.
- **Engrave layers for part labels** on the flat-pack job (stroke font →
  `engrave_vector`), after the move above.
- **The PCB emit adapter,** built by ewod-pcb when it takes the item up:
  - edge-cut → `cut`;
  - NPTH drill → `drill`;
  - silk → `engrave_vector`;
  - the EWOD gasket outline → a `cut` layer with Cricut settings
    (gr451662).

  This thread reviews it. PCB keeps its own model for DRC and Gerber.

## Explicitly NOT in scope

- Doors, drawers, hinges, hardware, finger pulls.
- Toolpaths or G-code. Export stops at the cut file; LightBurn, Cricut
  Design Space or the Maslow's own control takes it from there.
- Non-rectangular parts, curved shelves, mitres. Living hinges are
  `flatpack-living-hinges.md`.
- Multi-sheet layouts. The packer refuses, naming how much does not fit.
- Grain or flute direction optimisation.
- PCB reading from the sheet job, ruled out on se-machine-design-7.

## Open questions / decisions log

- **2026-10-03:** se-machine-design-4, -5 and -7 answered, as listed at the
  top.
- **Shelf joinery:** through-tabs into slots on every machine, because a
  laser cannot cut a dado. Dadoes become a Maslow-only option later.
- **Units:** mm only, inside the sheet job, the panel model and the
  generator's parameters.
- **Readiness vet, 2026-10-03:** the params home; the tab-in-slot check is
  flatpack's own, because `envelope_overlaps` compares block envelopes, not
  cad solids; where the checks live, and their severities; the SVG layer
  encoding; the material and load parameters; the panel table; the nesting
  numbers; the kerf join style; DXF R12 units; the cut-view body; re-run
  semantics.
- **Builds 1–2, 2026-10-09:** the sheet-job frame is x right, y up, origin
  bottom-left (the SVG writer flips y); the laser exporter's per-op
  defaults are starting points for 3 mm cardboard, not a material record;
  the generator picks the material figure by machine — `laser` at
  `cutter_d = 0`, `cnc` otherwise; shelves are refused when a slot would
  leave less than `t` of web (height within `[t, H−4t]`, spacing `≥ 2t`,
  `D ≥ 11t + 2·fit`); nesting with `sheet=None` tries one strip width per
  "k parts side by side" and keeps the smallest sheet.
