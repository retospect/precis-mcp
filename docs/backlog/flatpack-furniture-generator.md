---
status: ready
title: Flat-pack furniture generator on a shared sheet job — the se op, params home, view='cut', store-aware checks and the cardboard cut (builds 1–2 shipped)
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

## What shipped (builds 1 and 2)

`src/precis/sheet/` (model, validation, laser SVG and DXF R12 exporters)
and `src/precis_se/flatpack/` (materials, panel model with finger/straight
joints, shelves, `fit`, dog-bones, mitre kerf, skyline nesting,
`flatpack_box(...)` emitting the sheet job). The geometry rules and the
exporter contract live in those packages' docstrings; the acceptance
figures (the panel table, 5 × 10 mm fingers, the kerf area formulas, the
10 000 mm² shortfall) are pinned in `tests/test_flatpack_panels.py`,
`tests/test_flatpack_nest.py` and `tests/test_sheet_job.py`; the 50 mm
cube's SVG is the golden `tests/fixtures/flatpack/cube50_laser.svg`. Skill
`precis-se-flatpack-help` describes the python surface. No agent surface
yet: that is build 3 below.

## Design — the agent surface (build 3)

- **An se op**, `{"op": "flatpack_box", "params": {...}}`, sent through
  `put(kind='se')`. It is handler-level beside `realize` and `generate`
  (`precis_se/atomic/apply.py::HANDLER_LEVEL_OPS`).
  - It mints one cad design per panel and one block per panel, placed, plus
    the rigid connects. Commits are deferred until the whole ops list
    validates, as `realize` does.
  - The cad design is a `SceneSpec` built from a box, with `add` fingers
    and tabs, `cut` slots and `cut` dog-bone cylinders, following
    `precis_se/realize.py::prepare_realize`/`finish_realize` with one
    pending per panel. The panel's `Frame` (origin, u, v, n) is the pose.
  - Mated panel pairs are declared with a `connect` of joint class
    `rigid`, so `validate.py::envelope_overlaps` treats their seam overlap
    as sanctioned.
  - Cad slugs are `<design>-<panel>`. Running the op again on the same
    design replaces those cad designs under the same slugs and rebinds the
    blocks. A panel that no longer exists, such as a removed shelf, has its
    cad design retired. Nothing is suffixed `-2`, and nothing is orphaned.
  - `generate` stays the atomic-structure path: its builders return a
    `GeneratedBlock` structure, not a set of cad panels.
- **Where the params live:** in `refs.meta['flatpack']` on the se design —
  the `Flatpack.params` dict the generator already echoes (JSON-shaped).
  - `apply_ops_with_atomic` hands them back through a new out-parameter, as
    it hands back `pending_jobs`.
  - `put` merges them into the meta it writes. A put whose ops include no
    `flatpack_box` keeps an existing `meta['flatpack']`.
  - An `edit` that runs the op writes them with `meta || jsonb`.
  - Every export regenerates the panels from these params. No migration.
- **The cut view:** `get(kind='se', id=..., view='cut', args={'format':
  'svg'|'dxf'})`.
  - It takes `format`, matching se's `view='export'`.
  - It is registered in `precis_se/handler.py::_VIEW_ARGS`, with its own
    branch. It is a sibling of the nucleic `chain/export.py::render_export`,
    not a branch inside it.
  - The body is the file only. The notes already go in the exporters'
    leading comment (SVG `<!-- -->`, DXF group 999): parts, sheet size,
    utilisation, the colour → settings table, the figures used and their
    source, and any refused layer.
- **Skills:**
  - `precis-se-help` gains the op and the view in its tables.
  - `precis-se-flatpack-help` gains the op, the view and the fenced
    examples once they exist.

## Checks (build 3)

The checks live in a new `precis_se/flatpack/checks.py::findings(store,
tree, ref_id)`. It reads `meta['flatpack']`, rebuilds the panel model, and
returns `ValidationIssue`s (`precis_se/validate.py`).
`precis_se/handler.py::_render_drc` appends them after the store-aware
passes. `drc.py::drc(tree)` stays store-free.

Severity `error`:
- **Mating.** Every tab or finger is transformed into its mate panel's
  frame and must lie inside the mate's slot or gap polygon, with clearance
  `fit` on each side, within 1e-6 mm. Every slot has a tab. (The
  ownership-tiling test in `tests/test_flatpack_panels.py` is the
  store-free form of this; the check reuses `Panel.frame`.)
- **Minimum width.** No part is narrower than `2t` anywhere: shapely
  `buffer(-t)` leaves a non-empty polygon.
- **Nesting.** No two nested parts overlap after kerf, and every part lies
  inside the sheet margin.

Severity `warn`:
- **Shelf sag** when `shelf_load_n` is set: a simply supported beam with
  the material's `E`. It warns past 3 mm. If the load is unset, or the
  material has no `E`, the finding says it skipped and why rather than
  inventing a figure. (`flatpack_box` already accepts and echoes
  `shelf_load_n`.)
- **No recorded figure.** A material with no recorded `fit` or `kerf` for
  the machine names the value it used (the generator's `FigureEcho`).

Severity `info`:
- Sheet utilisation, always.

## Builds

3. **se design, checks and the cardboard cut.**
   - The se op, the params home, the `view='cut'` surface, the checks
     above, and both skills.
   - It lands on the software criteria. Then a review-queue item hands Reto
     the cube's SVG for the physical cut.

Later, each its own build or less, none blocking build 3:
- **Cricut SVG exporter.** Close to free on top of the laser writer: the
  SVG restricted to `cut` and `score`, plus `pressure` and `blade` per
  layer from the layer's `cricut` settings. Any other op is refused by
  name.
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

## Acceptance criteria (build 3)

- The se op realises the 50 mm cube as 5 placed blocks with rigid connects.
- `view='drc'` reports zero flatpack mating findings and zero
  `undeclared_interpenetration` between mated panels.
- Running the op again with `shelves=[22]` adds one block and one cad
  design. It suffixes nothing and orphans nothing.
- `view='cut'` returns a valid SVG and a valid DXF. The notes give the
  parts, the sheet size, the utilisation and the colour table, and say that
  `fit` and `kerf` are 0 mm with no recorded figure.
- **Physical check.** This is Reto's, and it is not agent-gradable. It is
  handed over as a review-queue item after build 3 lands.
  - Reto cuts the 50 mm finger-joint cube in 3 mm corrugated cardboard on
    his laser.
  - The `fit` and `kerf` that seat are recorded as `corrugated-3mm`'s laser
    figures in `materials.py` and the generator's docstring, in a follow-up
    commit.
  - If the first cut does not seat, the record says what changed, and the
    figures that seated are the ones recorded.
  - The item is deleted when that commit lands.

## Target + blast radius (build 3)

- `precis_se/flatpack/checks.py`, new.
- `precis_se/atomic/apply.py`: the handler-level op and the params
  out-parameter.
- `precis_se/handler.py`: the meta merge on put and edit, `view='cut'` in
  `_VIEW_ARGS`, and the checks appended in `_render_drc`.
- Skills: `precis-se-help`, `precis-se-flatpack-help`.
- No migrations, no worker changes, no new dependency.

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
