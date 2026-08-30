---
status: draft
title: One courtyard polygon — hull-derived, SAT-tested, fab-derived clearance
prio: high
---

# One courtyard polygon — hull-derived, SAT-tested, fab-derived clearance

## Motivation / why

A part's courtyard exists **five** times, computed independently, and the one
drawn on the board is not the one enforced:

| # | definition | shape | consumer |
|---|---|---|---|
| 1 | `ir.instance_pad_radius` | — | shared primitive: centre → outermost **pin centre** |
| 2 | `ir.instance_keepout_radius_mm` = #1 + `PAD_BREATHING_MM` (0.6), floored at the caller's `min_radius_mm` | circle | placer legality, seeder, `handlers/pcb.py::_drc_geometry` → `check_courtyard_overlap` **and** `check_outline_containment` |
| 3 | `silk._courtyard_reach_mm` = #1 + `half_pad` + `COURTYARD_MARGIN_MM` (0.25) | square | the silk actually **printed** |
| 4 | `drc.DEFAULT_COURTYARD_RADIUS_MM` = 1.0 flat | circle | fallback; base of `cost.COURTYARD_MIN_SEPARATION_MM` |
| 5 | `cost.courtyard_overlap_pair_term`: flat `COURTYARD_MIN_SEPARATION_MM` (2.0) centre-distance | circle | the anneal's graded `courtyard_overlap` margin term (incremental cache: `optimize._init_courtyard_state`) |

Two docstrings claimed to be "the ONE definition"
(`handlers/pcb.py::_drc_geometry`, `optimize.py`); they drifted; both
corrected 2026-08-29.

**A circle cannot stand in for the rectangle.** Measured, current constants —
the drawn square's corners escape the reserved circle for every part, and the
error changes sign with aspect ratio:

```
part                   rect w x h  rect mm2  circle mm2   waste
0402 cap            1.95 x  1.45       2.8         3.8    1.3x
SOIC-8              6.80 x  5.90      40.1        28.3    0.7x  UNDER-reserves
TO-220              7.58 x  7.10      53.8        31.0    0.6x  UNDER-reserves
hdr 1x8 2.54       19.98 x  4.74      94.7       282.9    3.0x  over-reserves
edge conn 1x20     50.46 x  4.74     239.2      1921.3    8.0x  over-reserves
```

No single radius fixes both: 8x over-reservation spreads the board around
slivers while chunky parts under-reserve and their outlines collide.

`COURTYARD_MARGIN_MM = 0.25` is also the wrong kind of number: IPC-7351
*assembly* excess, where the governing constraint is fab printability — silk
must clear the soldermask **opening**, already swelled past the copper.

## In scope

1. **One courtyard polygon, owned by `ir.py`.** Convex hull of the
   instance's own pad **outlines** (exact: this IR has no per-pin rotation,
   pads are axis-aligned in the footprint frame — see
   `instance_pad_radius`'s docstring), offset outward by the derived
   clearance (5). Non-convex packages are conservatively over-covered.
   Pinless instances (mounting hole, fiducial) keep a declared fallback
   shape — what today's `min_radius_mm` floor really does.
   `hull(own pad outlines) ⊕ margin` cannot overlap its own pads: 18 of 22
   reference-board drops were exactly that and become unrepresentable, not
   "fixed by a good constant".

2. **`silk.py` draws that polygon** instead of its own square. Drawn region
   and reserved region become the same object.

3. **`optimize.py` tests polygon overlap (SAT), batched numpy**, replacing
   `dist(centres) >= r_i + r_j`. Broad-phase radius, if kept at all, is
   *derived* from the polygon (406 pairs on the 29-part board — it may not
   earn its keep). Rotation is a 2x2 matmul on hull vertices, any angle.
   `silk._polygons_overlap` is already an exact convex SAT — share one
   polygon-overlap primitive; a second independent implementation is the
   two-call-sites defect this item exists to remove.

4. **`_drc_geometry` hands the same polygon** to `check_courtyard_overlap`
   and `check_outline_containment` — leaving DRC on circles while the
   placer goes polygon re-creates the drift.

5. **Derive the clearance from the fab chain, not a convention:**

   ```
   pad copper edge
     + soldermask expansion    → mask opening edge
     + silk-to-mask clearance  → silk line edge
     + drawn stroke width / 2  → silk CENTRELINE
   ```

   Add two `capabilities.FIELDS`: `soldermask_expansion_mm` (today a
   hardcoded `gerber.SOLDERMASK_EXPANSION_MM = 0.05`, applied per side via
   `_expand_pad`) and a silk-to-mask clearance (exists nowhere under any
   name). `gerber.py`'s mask writer must consume the new expansion field —
   otherwise the drawn mask and the derived clearance are two expansions
   that drift. Pin which tier the chain reads (`jlc_min` vs
   `house_default`). Values looked up per process, null where JLC publishes
   none (aluminum, per the JSON's own convention) — the chain must define
   its null fallback. The last term is the *drawn* stroke width
   (`gerber.DEFAULT_SILK_WIDTH_MM`, 0.15) — `capabilities.silk_width_mm` is
   the printability *floor*, numerically coincident today only. Chain total
   ~0.275mm at JLC-typical numbers (0.05 + 0.15 + 0.075): today's 0.25 is
   marginally under spec.

6. **Fix the mask-opening under-check.** `silk._stroke_overlaps_any_pad`
   tests silk against pad **copper**; the constraint is the mask *opening*,
   0.05mm larger per side. Every silk clearance check in that module is
   ~0.05mm optimistic.

## Explicitly NOT in scope

- **Non-convex courtyards / exact package outlines.**
- **Placer awareness of vias or fiducials.** Three reference-board drops
  (`D2`, `R2`, `U1`) are a part's own plane-fanout vias (router-placed,
  after placement is final); one (`J2`) is a render-time fiducial. No
  amount of shoving fixes these — separate items.
- **A `soldermask_dam_mm` DRC rule.** The field is genuinely dead
  (allow-listed in `tests/test_pcb_dead_exports.py`); adjacent-opening mask
  slivers are a real check, not this one.
- **Courtyard relocation/clipping in `build_silk`.** It drops the whole
  outline on any overlap, no ladder. After this item the remaining drops
  should be few and each a real finding; revisit only if the count stays
  high.
- **Retiring `DEFAULT_COURTYARD_RADIUS_MM` / reworking `cost`'s graded
  courtyard term.** The anneal keeps steering by the flat-circle term (#5
  in the table) while legality goes polygon — accepted mismatch for this
  slice; see open question.

## Acceptance criteria

- `ir.py` exposes one courtyard-polygon function; `silk.py`, `optimize.py`
  and `_drc_geometry` consume it. No second reach/extent formula survives
  in `silk.py`; `tests/test_pcb_dead_exports.py` green without a new
  allow-list entry.
- Property test: randomized instances (pad counts, sizes, offsets,
  rotations) — the courtyard polygon never intersects its own pads at
  nonzero stroke width. Pins the 18/22 class structurally. Fixtures must
  be asymmetric (non-square hull, off-centre pads): a symmetric fixture
  cannot see a 90-degree or sign error.
- SAT legality agrees with a brute-force oracle over randomized polygon
  pairs; independent implementations. The exact-touch verdict (zero-area
  contact) is pinned once, strict-or-not, and touching cases are
  *constructed* (shared coordinates), not sampled — a sampled touch is
  measure-zero and never fires.
- A known-overlapping pair produces a `courtyard_overlap` finding through
  the polygon path — the check is skipped entirely when the courtyards
  list is empty (`run_geometric_drc`), so "0 findings" alone proves
  nothing.
- Rotation: an arbitrary-angle part's courtyard rotates through the same
  single affine path as its pads. `padplace._rotate_cw` and
  `landpattern.rotate_offset` spell out the identical
  `[[cos, sin], [-sin, cos]]` matrix (`rotate_offset` also folds
  mirror-before-rotate); this is where they become one — coordinate with
  `pcb-engine-plan.md`'s "one affine-transform path", which tracks the
  same unification.
- Clearance comes from the capability chain: tests assert that changing a
  process row's silk-to-mask value AND its soldermask-expansion value each
  move the courtyard — both new fields provably consumed, not declared.
- Measured before/after on the reference board, by rule: `silk_missing`
  (27 at natural size, 61 on the squeezed 40mm fixture as of `f84efa5c`),
  `courtyard_overlap`, `clearance`, routed/unrouted split. A `silk_missing`
  drop bought by *not checking* is the failure mode to watch: the count
  means nothing without the routed count beside it.

## Target + blast radius

`src/precis/pcb/ir.py`, `silk.py`, `optimize.py`, `drc.py` (polygon
courtyard in `check_courtyard_overlap`/`check_outline_containment`),
`capabilities.py`, `gerber.py` (expansion field),
`src/precis/data/pcb_capabilities.json`, `padplace.py`/`landpattern.py`
(rotation unification), `handlers/pcb.py::_drc_geometry`. `cost.py` reads
`DEFAULT_COURTYARD_RADIUS_MM` — check it is undisturbed.

Touches the placement engine: `op='place'`/`op='route'` results move
for every board.
`tests/test_pcb_reference_end_to_end.py::BASELINE_DRC_ERRORS` and the 40mm
fixture (`tests/test_pcb_fab_render_all_layers.py`) both shift.

## Open questions / decisions log

- **DECIDED (user, 2026-08-29): full arbitrary/convex shape, not a bigger
  circle.** Radii are not a source of truth. An earlier note claiming
  rotation becomes expensive conflated compute cost with search dynamics;
  the only residue is that anneal parameters were tuned when ROTATE could
  not affect legality.
- **OPEN — does the 40mm reference fixture still place?** Chunky parts
  under-reserve today, so honest polygons need more room. If it stops
  placing that is a true finding, but measure it up front; then decide
  whether the fixture grows or the item narrows.
- **OPEN — real values for the two new capability fields.** Looked up per
  process. JLC-typical silk-to-mask ~0.15mm, expansion ~0.05mm are
  starting points, not answers.
- **OPEN — keep a broad phase at all?** Derive the bound from the polygon;
  keep only if profiling asks.
- **OPEN (main loop) — the anneal's graded courtyard term** (#5, flat
  2.0mm centre-distance) now disagrees with polygon legality: the
  optimizer descends a slope defined by a different courtyard than the one
  enforced. Accept for this slice, or move the term to polygon overlap
  depth?
- **OPEN (main loop) — split?** Items 5+6 (capability fields +
  mask-opening fix) are independently shippable and small; items 1-4 move
  every board's placement and hang on the 40mm question. Candidate: ship
  5+6 first, 1-4 `blocked-by` it.
