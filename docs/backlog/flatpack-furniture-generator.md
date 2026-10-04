---
status: ready
title: Flat-pack furniture generator on a shared sheet job — a box with shelves, joinery from sheet thickness, nested and exported for laser, CNC and (later) Cricut
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

## Motivation / why

The se kinds realise machines as solids, and the organic-print route
realises them as printed fields (`structural-solution-space.md` §Slice 4
bridge). Neither produces the third common way a maker builds a thing: flat
parts cut from one sheet and slotted together. A box with shelves is the
smallest useful member of that family. It needs exactly these and nothing
else:
- part outlines from a 3-D intent;
- joinery derived from the sheet thickness;
- kerf;
- nesting;
- a cut file a machine accepts.

The sheet job is the 2-D artefact underneath. Reto's three 2-D machines
want the same thing: a planar job of layers, each layer one operation with
settings per machine. The three are:
- the laser;
- the Cricut;
- PCB's mechanical layers: edge cuts, NPTH drills, and the EWOD gasket of
  gr451662.

Writing the exporters once against that model costs the same as writing
flat-pack-only writers. The PCB and Cricut cases then need no second
writer.

## Design

### Sheet job (`src/precis/sheet/`, new neutral package)

The package holds pure data and pure functions: no DB and no handler. It
imports nothing from `precis.pcb` or `precis_se`, so both can emit into it.
The import-linter contracts in `pyproject.toml` allow it as is.

- **Sheet:** width and height in mm, material name, thickness.
- **Layers**, in order. Each layer has a name and exactly one `op`: `cut`,
  `score`, `engrave_vector`, `engrave_raster`, or `drill`.
  - `engrave_raster` is a reserved kind in build 1: the model accepts it,
    and every build-1 exporter refuses it by name.
  - Default layer order, the order a machine runs them: `engrave_raster`,
    `engrave_vector`, `score`, `drill`, `cut`. Parts are cut free last.
- **Per-machine parameters** on a layer: a mapping keyed by machine.
  - `laser: {power_pct, speed_mm_s, passes}`
  - `cricut: {pressure, blade}`
  - `pcb: {fab_layer}`

  A layer may carry several machines' settings, so one job feeds the laser
  or the Cricut unchanged. An exporter reads only its own machine's key. A
  layer with no key for that machine exports with the machine's defaults,
  and the export notes say so.
- **Shapes:** closed or open polylines and circles, in mm. Each sits on one
  layer, may carry a part id, and is flagged inner or outer for its part.
- **Placed images** (raster build only): the image, a placement rectangle,
  and the dpi. The laser layer's power range maps grey to power.
- **Parts:** id, name, and an optional 3-D placement (pose plus
  thickness).

Exporters are one module each. Each takes the operations its machine can
do and refuses the rest by layer name; it never drops a layer silently.
- **Laser SVG.** Every op except raster in build 1.
  - Units are mm in `viewBox` and width/height, with hairline strokes.
  - Each layer is one
    `<g id="{name}" stroke="#rrggbb" data-op="…" data-power-pct="…" data-speed-mm-s="…" data-passes="…">`.
  - Layer *i* takes colour *i* of a fixed palette, LightBurn's first
    colours in order. LightBurn maps colour to its own layer and reads no
    settings from the file, so the `data-` attributes are for us. The cut
    view's notes carry a colour → settings table, so the operator can set
    up LightBurn.
  - Document order is the cut order. Layers come in job order. Within a
    layer, parts come in nesting order, and within a part, the inner
    contours come before the outline.
- **DXF.** Hand-written ASCII DXF R12 with `LINE`, `POLYLINE`/`VERTEX` and
  `CIRCLE` entities.
  - Each vector layer of the sheet job becomes one DXF layer of the same
    name.
  - A `drill` shape is written as a `CIRCLE` on its layer. Raster is
    refused.
  - R12 has no units header. The units are mm by convention, stated in a
    group-999 comment.
  - No new dependency: the tree has no DXF writer and no `ezdxf`.
- **Cricut SVG** (follow-on, see Builds). `cut` and `score` only, with
  pressure and blade per layer. Any other op is refused by name.

Geometry uses `shapely` (already a core dependency) for kerf offsets,
containment and overlap. The 2-D helpers in `precis/pcb/geom.py`
(`rounded_polygon`, `fillet_polyline`) and `precis/pcb/stroke_font.py` move
to the neutral package later (see Builds). Build 1 does not need them.

### Panel model (`src/precis_se/flatpack/`, new)

The source of truth is a 2-D panel model, not a 3-D solid. The thread file
argued this on 2026-10-03: deriving outlines from a solid needs a
section-to-polygon extractor cad does not have. Each panel is a rectilinear
outline with its fingers, slots and dog-bones, plus its 3-D placement. Two
things derive from it:
- **The sheet job:** nested panels on one `cut` layer. Later builds add
  engrave layers for part labels.
- **The se design:** one block per panel, each bound to a cad design.
  - The cad design is a `SceneSpec` built from a box, with `add` fingers and
    tabs, `cut` slots and `cut` dog-bone cylinders. These follow the
    pattern of `precis_se/realize.py::prepare_realize`/`finish_realize`,
    with one pending per panel.
  - Mated panel pairs are declared with a `connect` of joint class
    `rigid`. That way `validate.py::envelope_overlaps` treats their seam
    overlap as sanctioned and does not report it.

`flatpack_box(W, H, D, *, material='corrugated-3mm', t=None,
shelves=[], joint='finger', fit=None, kerf=None, cutter_d=0, sheet=None,
shelf_load_n=None)`. All lengths are in mm, and mm only.

- **Materials** are a constants module, `precis_se/flatpack/materials.py`,
  changed by commit, not a `kind='material'` row.
  - Each entry holds `t_nominal`, `t_actual`, and the per-machine `fit` and
    `kerf` figures once recorded, plus `E` where known.
  - `corrugated-3mm` comes first. Its laser `fit` and `kerf` are null until
    Reto's first cut. Its `E` stays null, and the entry notes that the
    board is stiffer along its flutes.
  - Plywood 6, 9, 12 and 18 mm follow for the Maslow, with `E = 9 GPa`.
- `t` defaults to the material's `t_actual`.
- An explicit `fit` or `kerf` overrides the record. A null in both falls
  back to 0 mm, and the echo says which figure it used and where it came
  from.
- **Box:** the outer dimensions are `W × H × D`, with an open front. The
  panels are two sides, top, bottom and back, plus one shelf per entry in
  `shelves`. A shelf height is measured from the inside bottom to the
  shelf's underside.
- **Finger joints** (`joint='finger'`) on every edge where two carcass
  panels meet: side–top, side–bottom, side–back, top–back, bottom–back.
  - Each panel's extent, finger tips included, is its box face.
  - An edge of length `L` gets `n` fingers: the odd number nearest
    `L / 3t`, at least 3, each `L/n` wide.
  - The generator refuses an edge where `L/3 < 2t`, naming the edge and the
    minimum length `6t`.
  - On each edge, the panel ranked first in the order side > top/bottom >
    back owns the odd positions (1, 3, …, n), which are the end fingers.
  - Where three panels meet (side, top or bottom, and back), the
    `t × t × t` corner cube belongs to the side. On the top–back and
    bottom–back edges, the owning panel's end fingers therefore stop `t`
    short of each end. Every corner cube belongs to exactly one panel.
  - Finger depth is `t`. Each gap a finger enters is widened by `fit`.
- **Straight joints** (`joint='straight'`): plain butt edges, for gluing.
  The sides are full size `D × H`. Top and bottom sit between the sides at
  `(W−2t) × D`. The back sits between the sides and between top and bottom
  at `(W−2t) × (H−2t)`, flush with the rear face.
- **Shelves**, for both joint types:
  - the body is `(W−2t) × (D−t)` and stops at the back;
  - each side edge carries two tabs, `3t` wide and `t` long, centred at ¼
    and ¾ of the depth;
  - each side gets matching through-slots of `(3t + fit) × (t + fit)`.

  A shelf uses tabs and slots on every machine, because a laser cannot cut
  a dado.
- **Kerf:** every cut path moves `kerf/2` away from the part's material.
  An outline moves outward; a slot or hole contour moves into the opening.
  The offset uses shapely `join_style='mitre'`, so rectilinear corners stay
  square.
- **Dog-bones** go on every inside corner when `cutter_d > 0`, and none
  appear at `cutter_d = 0` (laser, Cricut). Each is a circle of radius
  `cutter_d/2`, centred on the corner's bisector, inside the removed
  region, with its edge passing through the corner point. It is added to
  the removed region.
- **Nesting:** a skyline rectangle packer over each part's bounding box
  after kerf.
  - Parts may rotate 90°.
  - Spacing between parts and the margin to the sheet edge are both exactly
    `max(cutter_d, kerf, 1 mm)`.
  - With `sheet=(w, h)` given, the packer fits everything or refuses. The
    shortfall it names is the sum of the unplaced parts' bounding-box areas
    in mm².
  - With `sheet=None` (Reto's laser bed is no constraint), it packs into a
    strip and reports the sheet it used: the bounding box of the placement
    plus the margin.

### Agent surface

- **An se op**, `{"op": "flatpack_box", "params": {...}}`, sent through
  `put(kind='se')`. It is handler-level beside `realize` and `generate`
  (`precis_se/atomic/apply.py::HANDLER_LEVEL_OPS`).
  - It mints one cad design per panel and one block per panel, placed, plus
    the rigid connects. Commits are deferred until the whole ops list
    validates, as `realize` does.
  - Cad slugs are `<design>-<panel>`. Running the op again on the same
    design replaces those cad designs under the same slugs and rebinds the
    blocks. A panel that no longer exists, such as a removed shelf, has its
    cad design retired. Nothing is suffixed `-2`, and nothing is orphaned.
  - `generate` stays the atomic-structure path: its builders return a
    `GeneratedBlock` structure, not a set of cad panels.
- **Where the params live:** in `refs.meta['flatpack']` on the se design.
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
  - The body is the file only. The notes go in a leading comment (SVG
    `<!-- -->`, DXF group 999): parts, sheet size, utilisation, the colour →
    settings table, the figures used and their source, and any refused
    layer.
- **Skills:**
  - `precis-se-help` gains the op and the view in its tables.
  - A new `precis-se-flatpack-help` covers the parameters, the materials and
    their recorded figures, the cut view, and the physical fit test.

## Builds

1. **Sheet-job core, laser SVG and DXF exporters.**
   - The model above, with validation: one op per layer, known ops only,
     every shape on a declared layer.
   - Both exporters, refusing `engrave_raster` by name.
   - Tests only; no agent surface yet.
2. **Panel model, joints and nesting.**
   - `flatpack_box` to panels: finger and straight joints, shelf tabs and
     slots, `fit`, `kerf` and dog-bones.
   - The materials module.
   - Nesting, and emission into a sheet job.
3. **se design, checks and the cardboard cut.**
   - The se op, the params home, the `view='cut'` surface, the checks
     below, and both skills.
   - It lands on the software criteria. Then a review-queue item hands Reto
     the cube's SVG for the physical cut.

Later, each its own build or less, none blocking the three above:
- **Cricut SVG exporter.** Close to free on top of build 1: the SVG writer
  restricted to `cut` and `score`, plus pressure and blade.
- **Grayscale raster engrave.** Image placement, the grey-to-power map, and
  embedding in the laser SVG. One build, when Reto wants images on panels;
  build 1 already reserves the layer kind.
- **The neutral move of the `pcb/geom.py` helpers and the stroke font** into
  `precis/sheet/`, with shims at the old paths. Half a build, owned by this
  thread, and sequenced with the parked pcb-easyeda-round-trip.
- **The PCB emit adapter,** built by ewod-pcb when it takes the item up:
  - edge-cut → `cut`;
  - NPTH drill → `drill`;
  - silk → `engrave_vector`;
  - the EWOD gasket outline → a `cut` layer with Cricut settings
    (gr451662).

  This thread reviews it. PCB keeps its own model for DRC and Gerber.

## Checks (build 3)

The checks live in a new `precis_se/flatpack/checks.py::findings(store,
tree, ref_id)`. It reads `meta['flatpack']`, rebuilds the panel model, and
returns `ValidationIssue`s (`precis_se/validate.py`).
`precis_se/handler.py::_render_drc` appends them after the store-aware
passes. `drc.py::drc(tree)` stays store-free.

Severity `error`:
- **Mating.** Every tab or finger is transformed into its mate panel's
  frame and must lie inside the mate's slot or gap polygon, with clearance
  `fit` on each side, within 1e-6 mm. Every slot has a tab.
- **Minimum width.** No part is narrower than `2t` anywhere: shapely
  `buffer(-t)` leaves a non-empty polygon.
- **Nesting.** No two nested parts overlap after kerf, and every part lies
  inside the sheet margin.

Severity `warn`:
- **Shelf sag** when `shelf_load_n` is set: a simply supported beam with
  the material's `E`. It warns past 3 mm. If the load is unset, or the
  material has no `E`, the finding says it skipped and why rather than
  inventing a figure.
- **No recorded figure.** A material with no recorded `fit` or `kerf` for
  the machine names the value it used.

Severity `info`:
- Sheet utilisation, always.

## Explicitly NOT in scope

- Doors, drawers, hinges, hardware, finger pulls.
- Toolpaths or G-code. Export stops at the cut file; LightBurn, Cricut
  Design Space or the Maslow's own control takes it from there.
- Non-rectangular parts, curved shelves, mitres. Living hinges are
  `flatpack-living-hinges.md`.
- Multi-sheet layouts. The packer refuses, naming how much does not fit.
- Grain or flute direction optimisation.
- PCB reading from the sheet job, ruled out on se-machine-design-7.

## Acceptance criteria

### Build 1
- A sheet job with one layer per vector op exports a laser SVG:
  - one `<g>` per layer, with the palette colour and the `data-` settings;
  - mm units;
  - document order as specified.
- The same job exports a DXF whose layer names match the job's vector
  layers. A reader written in the test, not added as a dependency, parses
  every entity back to within 1e-6 mm. `drill` comes back as `CIRCLE`.
- An `engrave_raster` layer is refused by both exporters, naming the layer.
- A layer with no settings for the exporting machine exports with the
  defaults, and the notes say so.

### Build 2
Panel figures for the 50 mm cube in `corrugated-3mm` (`t = 3`):

| panel | finger (extent) | straight |
|---|---|---|
| side ×2 | 50 × 50 | 50 × 50 |
| top, bottom | 50 × 50 | 44 × 50 |
| back | 50 × 50 | 44 × 44 |
| shelf (if any) | 44 × 47 body + 4 tabs 9 × 3 | same |

- `flatpack_box(50, 50, 50)` emits 5 panels with the finger figures. Every
  edge has 5 fingers of 10 mm, and every corner cube belongs to exactly
  one panel.
- `joint='straight'` emits the straight figures.
- Changing `t` from 3 to 6 changes every finger count, finger width, slot
  and tab per the rules, with nothing hand-edited. A 12 mm edge at `t = 3`
  is refused, naming the 18 mm minimum.
- **Kerf, outline.** With `kerf` 0.2 (`d = 0.1`), each outline's area grows
  by exactly `P·d + 4d²`, within 1e-6 mm². This holds for any rectilinear
  outline with mitre joins.
- **Kerf, opening.** Each slot's area shrinks by `P·d − 4d²`.
- **Dog-bones.** Each inside corner gets a circle of radius `cutter_d/2`
  when `cutter_d > 0`, placed per the rule. There are none at
  `cutter_d = 0`.
- **Nesting.** At `kerf = 0` and `sheet=(60, 60)`, the packer places one
  panel and refuses, naming a shortfall of 10 000 mm². With `sheet=None`,
  it reports the sheet it used, and the parts do not overlap.
- `shelves=[22]` adds a sixth panel with 4 tabs, and two matching slots in
  each side.

### Build 3
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

## Target + blast radius

- New packages:
  - `src/precis/sheet/`: the model and the exporters;
  - `src/precis_se/flatpack/`: the panel model, nesting, materials and
    checks.
- `precis_se/atomic/apply.py`: the handler-level op and the params
  out-parameter.
- `precis_se/handler.py`: the meta merge on put and edit, `view='cut'` in
  `_VIEW_ARGS`, and the checks appended in `_render_drc`.
- Skills: `precis-se-help`, and the new `precis-se-flatpack-help`.
- No migrations, no worker changes, no new dependency. Rectangle nesting is
  the only non-trivial algorithm, and rectangle packing is enough for a
  box.

## Open questions / decisions log

- **2026-10-03:** se-machine-design-4, -5 and -7 answered, as listed at the
  top.
- **Shelf joinery:** through-tabs into slots on every machine, because a
  laser cannot cut a dado. Dadoes become a Maslow-only option later.
- **Units:** mm only, inside the sheet job, the panel model and the
  generator's parameters.
- **Readiness vet, 2026-10-03:** these points were folded in:
  - the params home;
  - the tab-in-slot check is flatpack's own, because `envelope_overlaps`
    compares block envelopes, not cad solids;
  - where the checks live, and their severities;
  - the SVG layer encoding;
  - the material and load parameters;
  - the panel table;
  - the nesting numbers;
  - the kerf join style;
  - DXF R12 units;
  - the cut-view body;
  - re-run semantics.
