---
status: draft
title: Courtyard polygon — remaining slice: put the PLACER and DRC on it too
prio: high
---

# Courtyard polygon — remaining slice: put the PLACER and DRC on it too

**Items 1, 2, 5 and 6 of this item shipped 2026-08-30.** What follows is
the residue: items 3 and 4, plus two measured populations the shipped work
narrowed but did not close. The motivation table below still holds for
the definitions that remain unmerged.

## What shipped (do not redo)

- **One courtyard polygon, owned by `ir.py`**
  (`instance_courtyard_polygon`): the convex hull of the instance's own
  pad outlines, offset outward by the fab-derived clearance, mitre-joined
  with a limit. `hull(own pads) + clearance` cannot overlap those pads —
  structural, pinned by a randomized property test in
  `tests/test_pcb_ir.py`. Pinless instances get `[]`; the caller supplies
  its own fallback body.
- **`silk.py` draws that polygon.** `_courtyard_reach_mm`,
  `_courtyard_box` and `COURTYARD_MARGIN_MM` are gone. The pin-1 tick cuts
  a hull VERTEX; refdes candidates are offset by the polygon's support in
  their own direction (`_courtyard_support_mm`) instead of one radius.
- **Clearance from the fab chain** (`silk.silk_clearance_mm`): mask
  expansion + silk-to-mask + half the DRAWN stroke.
  `capabilities.FIELDS` gained `soldermask_expansion_mm` and
  `silk_to_mask_clearance_mm`; `capabilities.design_value` pins the tier
  (`house_default`, then `jlc_min`, then the consumer's own named
  constant for a `None`). `gerber.soldermask_gerber` draws the mask film
  from `model["soldermask_expansion_mm"]`, resolved through the SAME
  `silk.soldermask_expansion_mm` the silk side reads.
- **The mask-opening under-check** (`silk._mask_openings`): every silk
  clearance test now runs against the pad's mask OPENING, not its copper.
  Vias are excluded — they are tented, so the bare annulus is the real
  obstacle.
- **Outlines break instead of dropping** (`silk._clip_polyline`). Not in
  the original scope; it turned out to be where the population actually
  lived. See "What the measurement said" below.

Measured, by rule, on the two fixtures the acceptance criteria named:

| board | `silk_missing` before | after | courtyard drops after |
|---|---|---|---|
| ESP32-C3 reference, natural size | 27 | **9** | 0 |
| 40mm squeezed fixture | 61 | **25** | 0 |

## What the measurement said (read before item 3)

The **first** cut of this work — honest polygons, no clipping — made both
numbers WORSE (27 → 32, 61 → 64). The reason is worth keeping: the old
radius-derived square was so oversized that it *enclosed* a part's own
plane fan-out vias. A tight, honest courtyard stops enclosing them and
starts passing THROUGH them, so every one became a drop. The polygon was
right; dropping a whole outline over one via was the actual defect, and
the "Explicitly NOT in scope" note below (courtyard relocation/clipping,
"revisit only if the count stays high") is what the count then asked for.

This also answers the item's own **OPEN — does the 40mm fixture still
place?**: yes, unchanged, and its DRC is strictly better.

## Still in scope

3. **`optimize.py` tests polygon overlap (SAT), batched numpy**,
   replacing `dist(centres) >= r_i + r_j`. Broad-phase radius, if kept at
   all, is *derived* from the polygon (406 pairs on the 29-part board — it
   may not earn its keep). Rotation is a 2x2 matmul on hull vertices, any
   angle. `silk._polygons_overlap` is already an exact convex SAT — share
   one polygon-overlap primitive; a second independent implementation is
   the two-call-sites defect this item exists to remove.

4. **`_drc_geometry` hands the same polygon** to `check_courtyard_overlap`
   and `check_outline_containment` — leaving DRC on circles while the
   placer goes polygon re-creates the drift.

## Residue the shipped work narrowed but did not close

- **Pin-1 ticks whose courtyard corner holds a fan-out via** (9 of the
  40mm fixture's 25, 1 of the reference board's 9). A tick is too small to
  break usefully — clipping it leaves debris, and shrinking it does not
  move it off a corner the via already occupies. The fix is a second
  marker convention (a dot beside pin 1, the other standard spelling)
  chosen when the corner is unavailable, not more geometry on the tick.
- **Refdes labels with nowhere to go** (16 of 25, 8 of 9). The 6-candidate
  ladder is exhausted; on the 40mm fixture that is substantially the
  fixture (~44mm of parts on a 40mm board), on the reference board it is
  not. Needs a real placement search, not another hardcoded candidate.

## Motivation / why (the definitions that remain)

Two of the five independent courtyard definitions are now one; three
remain, and the one the anneal steers by is still not the one enforced:

| # | definition | shape | consumer |
|---|---|---|---|
| 1 | `ir.instance_pad_radius` | — | shared primitive: centre → outermost **pin centre** |
| 2 | `ir.instance_keepout_radius_mm` = #1 + `PAD_BREATHING_MM` (0.6), floored at the caller's `min_radius_mm` | circle | placer legality, seeder, `handlers/pcb.py::_drc_geometry` → `check_courtyard_overlap` **and** `check_outline_containment` |
| 4 | `drc.DEFAULT_COURTYARD_RADIUS_MM` = 1.0 flat | circle | fallback; base of `cost.COURTYARD_MIN_SEPARATION_MM` |
| 5 | `cost.courtyard_overlap_pair_term`: flat `COURTYARD_MIN_SEPARATION_MM` (2.0) centre-distance | circle | the anneal's graded `courtyard_overlap` margin term (incremental cache: `optimize._init_courtyard_state`) |

**A circle cannot stand in for the rectangle.** Measured, current
constants — the drawn extent's corners escape the reserved circle for
every part, and the error changes sign with aspect ratio:

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

## Explicitly NOT in scope

- **Non-convex courtyards / exact package outlines.**
- **Placer awareness of vias or fiducials.** Router-placed fan-out vias
  land after placement is final; a render-time fiducial later still. No
  amount of shoving fixes these — see the pin-1 residue above.
- **A `soldermask_dam_mm` DRC rule.** The field is genuinely dead
  (allow-listed in `tests/test_pcb_dead_exports.py`); adjacent-opening
  mask slivers are a real check, not this one.
- **Retiring `DEFAULT_COURTYARD_RADIUS_MM` / reworking `cost`'s graded
  courtyard term.** The anneal keeps steering by the flat-circle term (#5)
  while legality goes polygon — accepted mismatch; see open question.

## Acceptance criteria

- `optimize.py` and `_drc_geometry` consume
  `ir.instance_courtyard_polygon`. No second reach/extent formula
  survives; `tests/test_pcb_dead_exports.py` green without a new
  allow-list entry.
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
- Measured before/after on both fixtures, by rule, against the numbers in
  the table above. A `silk_missing` drop bought by *not checking* is the
  failure mode to watch: the count means nothing without the routed count
  beside it.

## Target + blast radius

`src/precis/pcb/optimize.py`, `drc.py` (polygon courtyard in
`check_courtyard_overlap`/`check_outline_containment`),
`padplace.py`/`landpattern.py` (rotation unification),
`handlers/pcb.py::_drc_geometry`. `cost.py` reads
`DEFAULT_COURTYARD_RADIUS_MM` — check it is undisturbed.

Touches the placement engine: `op='place'`/`op='route'` results move for
every board. `tests/test_pcb_reference_end_to_end.py`'s baselines and the
40mm fixture (`tests/test_pcb_fab_render_all_layers.py`) both shift.

## Open questions / decisions log

- **DECIDED (user, 2026-08-29): full arbitrary/convex shape, not a bigger
  circle.** Radii are not a source of truth. An earlier note claiming
  rotation becomes expensive conflated compute cost with search dynamics;
  the only residue is that anneal parameters were tuned when ROTATE could
  not affect legality.
- **DECIDED (2026-08-30) — split.** Items 5+6 shipped alongside 1+2
  rather than ahead of them: the clearance chain is what SIZES the
  polygon, so shipping the polygon against the old flat constant would
  have measured the wrong thing. 3+4 stayed behind because they move
  every board's placement.
- **CLOSED (2026-08-30) — the 40mm fixture still places**, unchanged, and
  its DRC improved. See "What the measurement said".
- **OPEN — real values for the two new capability fields.** They are in
  the table at `medium` confidence (JLC's applied 0.05mm mask expansion,
  0.15mm silk-to-mask minimum) and were not re-verified against a live
  page on 2026-08-30; the rest of that row was checked on 2026-08-27.
- **OPEN — keep a broad phase at all?** Derive the bound from the
  polygon; keep only if profiling asks.
- **OPEN (main loop) — the anneal's graded courtyard term** (#5, flat
  2.0mm centre-distance) now disagrees with polygon legality: the
  optimizer descends a slope defined by a different courtyard than the one
  enforced. Accept for the next slice, or move the term to polygon overlap
  depth?
