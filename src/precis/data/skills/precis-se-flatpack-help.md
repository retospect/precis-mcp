---
id: precis-se-flatpack-help
family: se
title: precis — flat-pack boxes with shelves from one sheet (finger joints from the sheet thickness, nesting, laser SVG and CNC DXF)
summary: the flatpack generator turns W × H × D and a sheet material into panels with finger or butt joints and shelf tabs, nests them on a sheet with kerf and fit, and writes the laser SVG (LightBurn colours) or DXF R12 (Maslow) from one shared sheet job; which parameters exist, which figures are recorded per material, and how the first physical cut records fit and kerf
answers:
  - how do I make a laser-cut or CNC-cut box with shelves from 3 mm cardboard or plywood?
  - what do fit, kerf and cutter_d mean for a flat-pack part, and which values are recorded?
  - why did the generator refuse my edge, shelf or sheet?
  - what is the sheet job, and which machines export from it?
applies-to: the flat-pack generator under se — the flatpack_box op on put/edit, view='cut' for the sheet file, and the flatpack_* drc checks; read precis-se-help for the se design surface
status: active
tags: workflow, design
kinds: se
---

# precis-se-flatpack-help — a box with shelves, cut from one sheet

Where an se block becomes a solid and a print becomes a field, a
flat-pack box becomes panels: cut from one sheet, slotted together. The
generator is `precis_se.flatpack.flatpack_box`; its signature and callers
come from the python kind:

```python
get(kind='python', id='precis::precis_se.flatpack.generator.flatpack_box')
```

## Put a box on an se design — the flatpack_box op

`{"op": "flatpack_box", "params": {...}}` on `put`/`edit` of an se
design, where `params` are the generator's kwargs verbatim. One call
mints a `cad` design per panel under `<design>-<panel>` slugs (the panel
outline extruded: `add` boxes for the outline, `cut` boxes for slots,
`cut` cylinders for dog-bones), one placed block per panel, and a
`rigid` connect per mated pair. Re-running replaces the cad designs
under the same slugs and rebinds the blocks — nothing is suffixed `-2`,
and a removed panel's block and cad design are retired. The params land
in the design's `meta['flatpack']`, which every export and the drc
checks regenerate from.

```json
{"ops": [{"op": "flatpack_box",
          "params": {"W": 50, "H": 50, "D": 50,
                     "material": "corrugated-3mm", "shelves": [22],
                     "shelf_load_n": 5}}]}
```

## Get the cut file — view='cut'

```text
get(kind='se', id='cube50', view='cut', args={'format': 'svg'})
```

`format` is `svg` (LightBurn) or `dxf` (Maslow). The body is the file
only; the parts, sheet size, utilisation, colour→settings table and the
figures used (or "no recorded figure") are in its leading comment. A
design without `meta['flatpack']` is refused — run the op first.

`view='drc'` appends the `flatpack_*` checks: `flatpack_mating` (every
tab/finger in its mate's slot at the `fit` clearance; every gap filled),
`flatpack_min_width` (nothing narrower than 2t), `flatpack_nest_overlap`
/`flatpack_off_sheet`, `flatpack_shelf_sag` (warn past 3 mm, or says it
skipped), `flatpack_unrecorded_figure` (the `fit`/`kerf` used with no
material record), and `flatpack_utilisation` (info, always).

## What it makes

`flatpack_box(W, H, D, material='corrugated-3mm', t=None, shelves=[],
joint='finger', fit=None, kerf=None, cutter_d=0, sheet=None,
shelf_load_n=None, laser=None)` — all mm except `shelf_load_n` (newtons)
and `laser` (below). Bad parameters raise `FlatpackError`, a `BadInput`
(and `ValueError`) that names the rule: `shelves` must be a list of
heights (`[22]`, not `22`); `fit` and `kerf` must be at least 0 and below
`t`; `shelf_load_n` cannot be negative.
An open-front box `W × H × D` (width × height × depth): two sides, top,
bottom, back, one shelf per height in `shelves` (inside bottom to the
shelf's underside). Every figure derives from the sheet thickness `t`
(default: the material's actual thickness):

- **finger joints** (default) on every carcass edge. An edge of length
  `L` gets the odd number of fingers nearest `L / 3t`, at least 3, each
  `L/n` wide and `t` deep; the side owns the end fingers, then top and
  bottom, then back; the corner cube where three panels meet belongs to
  the side. Edges shorter than `6t` are refused by name.
- **straight joints** (`joint='straight'`): plain butt edges for glue.
  Sides `D × H`, top and bottom `(W−2t) × D` between them, back
  `(W−2t) × (H−2t)`.
- **shelves**: body `(W−2t) × (D−t)`, two `3t × t` tabs per side into
  through-slots of `(3t+fit) × (t+fit)` in the sides, on every machine
  (a laser cannot cut a dado). Refused when a slot would leave less than
  `t` of web to an edge, a finger gap or another slot.

The 50 mm cube in `corrugated-3mm` (`t = 3`) is the first box: five
50 × 50 panels, five 10 mm fingers on every edge; `shelves=[22]` adds a
44 × 47 shelf with four 9 × 3 tabs and two slots per side.

## fit, kerf, cutter_d

- `fit` widens every gap and slot a finger or tab enters, by `fit` in
  total (half a side). The tab itself does not change.
- `kerf` is the beam or cutter width: every cut path moves `kerf/2` away
  from the material (outline out, slot in), square corners kept.
- `cutter_d > 0` means a CNC: a dog-bone of radius `cutter_d/2` sits on
  every inside corner so a square mate seats. `0` is the laser or Cricut.

`sheet=(w, h)` is the sheet size in mm (see nesting below).
`shelf_load_n` (newtons per shelf, default `None`) is only recorded in the
result's `params` for the se build's shelf-sag check; this generator does
not compute with it, so it changes no panel.

Explicit `fit`/`kerf` win; otherwise the material's recorded figure for
the machine (`laser` at `cutter_d = 0`, `cnc` otherwise); otherwise 0 mm.
The result's `figures` and the job notes say which was used and why
("default: no recorded kerf for corrugated-3mm on laser").

## Materials and recorded figures

A constants module (`precis_se.flatpack.materials`), changed by commit:
`corrugated-3mm` (no `E`: the board is stiffer along its flutes), then
`plywood-6mm`, `-9mm`, `-12mm`, `-18mm` for the Maslow (`E = 9 GPa`).
Every recorded `fit` and `kerf` is still null and every actual thickness
equals the nominal: nothing has been cut and measured yet.

## The sheet job and its exports

The generator emits a **sheet job** (`precis.sheet`): sheet size and
material, ordered layers (one op each — `cut`, `score`, `engrave_vector`,
`drill`; `engrave_raster` is reserved and refused), per-machine settings
on a layer, shapes tied to parts. The box is one `cut` layer with each
part's slots before its outline, in nesting order. There is no `score`
layer: every joint is cut through, so the box has no fold or score line
(the exporter handles a score layer when a design adds one).

The `cut` layer carries the material's laser settings (`laser=` on the
call overrides per op and key: `laser={'cut': {'speed_mm_s': 8}}`;
keys `power_pct`, `speed_mm_s`, `passes`). For `corrugated-3mm` the
starting point is 100 % at 6 mm/s, one pass (score: 15 %, 120 mm/s) —
unmeasured, tune on the first cube. Plywood carries none (CNC), so a laser
export of it falls back to the exporter's generic defaults and says so.

- `precis.sheet.svg_laser.export_laser_svg` — mm units, one `<g>` per
  layer in LightBurn's palette order with `data-power-pct`,
  `data-speed-mm-s`, `data-passes`; the leading comment is the colour →
  settings table for the LightBurn setup (LightBurn reads none of it from
  the file). A layer without laser settings exports with the defaults and
  the notes say so.
- `precis.sheet.dxf.export_dxf` — DXF R12, one DXF layer per vector
  layer, circles for drills, mm by convention.

Nesting is a rectangle packer with 90° rotation; spacing and margin are
`max(cutter_d, kerf, 1 mm)`. With `sheet=(w, h)` it fits everything or
refuses, naming the unplaced parts and the mm² shortfall; with
`sheet=None` it packs a strip and reports the sheet it used.

## The physical fit test

Cut the 50 mm finger-joint cube in 3 mm corrugated cardboard, then
record the `fit` and `kerf` that seat as `corrugated-3mm`'s laser figures
in the materials module. If the first cut does not seat, record what
changed and the figures that did seat.

## See also

- [[precis-se-help]] — the se design surface the flat-pack op lives on
- [[precis-se-print-help]] — the printed route for the same block
