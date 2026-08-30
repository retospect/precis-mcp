---
status: draft
title: One courtyard polygon — hull-derived, SAT-tested, fab-derived clearance
prio: high
---

# One courtyard polygon — hull-derived, SAT-tested, fab-derived clearance

## Motivation / why

A part's courtyard exists **four** times in this subsystem, computed
independently, and the one drawn on the board is not the one enforced:

| # | definition | shape | consumer |
|---|---|---|---|
| 1 | `ir.instance_pad_radius` | — | shared primitive: centre → outermost **pin centre** |
| 2 | `ir.instance_keepout_radius_mm` = 1 + `PAD_BREATHING_MM` (0.6) | circle | placer, seeder, `courtyard_overlap` DRC |
| 3 | `silk._courtyard_reach_mm` = 1 + `half_pad` + `COURTYARD_MARGIN_MM` | square | the silk actually **printed** |
| 4 | `drc.DEFAULT_COURTYARD_RADIUS_MM` = 1.0 flat | circle | fallback; base of `cost.COURTYARD_MIN_SEPARATION_MM` |

Two docstrings asserted their own version was "the ONE definition"
(`handlers/pcb.py::_drc_geometry`, `optimize.py::_keepout_r`); the second
even named the failure it believed it prevented — "placement legality and
its DRC courtyard radius cannot silently drift apart". They drifted. Both
claims are corrected in place as of 2026-08-29.

**A circle cannot stand in for the rectangle.** Measured, with the current
constants — the drawn square's corners sit at `√2 ×` its half-extent and
escape the reserved circle for *every* part, and the error changes sign
with aspect ratio:

```
part                   rect w x h  rect mm2  circle mm2   waste
0402 cap            1.95 x  1.45       2.8         3.8    1.3x
SOIC-8              6.80 x  5.90      40.1        28.3    0.7x  UNDER-reserves
TO-220              7.58 x  7.10      53.8        31.0    0.6x  UNDER-reserves
hdr 1x8 2.54       19.98 x  4.74      94.7       282.9    3.0x  over-reserves
edge conn 1x20     50.46 x  4.74     239.2      1921.3    8.0x  over-reserves
```

Long parts get a disc reserved around a sliver (8× waste, forcing the board
to spread); chunky parts get *less* reserved than the courtyard needs, which
is why their outlines collide. **No single radius fixes both** — the error
is a function of aspect ratio, not scale, so inflating the constant cures
one failure and doubles the other.

The margin is also the wrong kind of number. `COURTYARD_MARGIN_MM = 0.25`
is IPC-7351's nominal courtyard excess — an *assembly* convention — where
the governing constraint is a *fab printability chain*: silk must clear the
soldermask **opening**, and the opening is already swelled past the copper.

## In scope

1. **One courtyard polygon, owned by `ir.py`.** Convex hull of the
   instance's own pad outlines, offset outward by the derived clearance
   (below). Convex covers round pads and odd packages uniformly; a
   non-convex package is conservatively over-covered, which is acceptable.
   Declared fallback shape for a pinless instance (mounting hole, fiducial)
   — that is what today's `min_radius_mm` floor is really doing and it must
   survive in some form.

   **This makes the dominant historical defect unrepresentable.** A
   courtyard derived as `hull(own pads) ⊕ margin` cannot overlap its own
   pads. 18 of 22 drops on the reference board were exactly that, and they
   stop being "fixed by a good constant" and become impossible.

2. **`silk.py` draws that polygon** instead of deriving its own square.
   Drawn region and reserved region become the same object; the divergence
   above stops existing rather than being documented.

3. **`optimize.py` tests polygon overlap (SAT) in numpy**, batched, instead
   of `dist(centres) >= r_i + r_j`. Radii are demoted to a *derived*
   broad-phase bound computed from the polygon (so it cannot drift), or
   dropped entirely — at 406 pairs for a 29-part board the broad phase is
   not yet earning its keep. Rotation is a 2×2 matmul on the hull vertices,
   free at any angle.

4. **Derive the clearance from the fab chain, not a convention:**

   ```
   pad copper edge
     + soldermask expansion   → mask opening edge
     + silk-to-mask clearance → silk line edge
     + silk_width_mm / 2      → silk CENTRELINE
   ```

   `capabilities.FIELDS` today carries `silk_width_mm` (0.15, min printable
   silk line) and `soldermask_dam_mm` (0.1, min mask web — **still dead**,
   no consumer). The other two terms are missing: soldermask expansion is a
   hardcoded `gerber.SOLDERMASK_EXPANSION_MM = 0.05` module constant rather
   than a per-process capability, and there is **no silk-to-mask clearance
   field anywhere**. Add both to `FIELDS` and to all three process rows
   (`2layer`, `4layer`, `aluminum`) with looked-up values, not invented
   ones. Note the chain totals ~0.275mm at JLC-typical numbers, so today's
   0.25 is marginally **under** spec.

5. **Fix the mask-opening under-check.** `silk._stroke_overlaps_any_pad`
   tests silk against **pad copper**; the thing silk must not touch is the
   **mask opening**, larger by the expansion per side. Every silk clearance
   check in that module is currently ~0.05mm optimistic.

## Explicitly NOT in scope

- **Non-convex courtyards / exact package outlines.** Convex hull only.
- **Making the placer aware of vias or fiducials.** Three reference-board
  drops (`D2`, `R2`, `U1`) are a part's own plane-fanout vias, which the
  *router* places after placement is final, and one (`J2`) is a fiducial
  placed at render time. **No amount of shoving fixes these** — they need
  the courtyard to model vias, and `build_fiducials` to see a provisional
  courtyard. Separate items.
- **A `soldermask_dam_mm` DRC rule.** Adjacent-opening mask slivers are a
  real check and that field is genuinely dead, but it is not this change.
- **Courtyard relocation or clipping.** `build_silk` drops the whole
  outline on any single overlap and has no ladder. Left alone deliberately:
  once the courtyard is hull-derived and the placer reserves it, the
  remaining drops should be few and each should be a real finding rather
  than something to route around. Revisit only if the count stays high.
- **Retiring `DEFAULT_COURTYARD_RADIUS_MM`.** `cost.py` builds
  `COURTYARD_MIN_SEPARATION_MM` on it; untangling that is separate.

## Acceptance criteria

- `ir.py` exposes one courtyard-polygon function; `silk.py` and
  `optimize.py` both consume it. No second reach/extent formula survives in
  `silk.py`, and `tests/test_pcb_dead_exports.py` stays green without a new
  allow-list entry.
- A property test: for a randomized instance (varied pad counts, sizes,
  offsets, rotations), the courtyard polygon **never** intersects that
  instance's own pads, at nonzero silk stroke width. This is the 18/22 class,
  pinned structurally.
- SAT legality agrees with a brute-force reference oracle over randomized
  polygon pairs, including the touching and near-touching cases. Producer
  and oracle must be independent implementations.
- Rotation: a part rotated by an arbitrary angle (not just 0/90/180/270)
  has its courtyard rotated by the same single affine path — no second
  rotation formula. `padplace._rotate_cw` and `landpattern.rotate_offset`
  are byte-identical duplicates today; this is where they become one.
- Clearance is computed from the capability chain. A test asserts that
  changing a process row's silk-to-mask value moves the courtyard, so the
  field is provably consumed rather than merely declared.
- **Measured before/after on the reference board**, reported by rule:
  `silk_missing` (27 at natural size, 61 on the squeezed 40mm fixture as of
  `f84efa5c`), `courtyard_overlap`, `clearance`, and the routed/unrouted
  split. A drop in `silk_missing` bought by *not checking* is the failure
  mode to watch for — the count means nothing without the routed count
  beside it.

## Target + blast radius

`src/precis/pcb/ir.py`, `silk.py`, `optimize.py`, `capabilities.py`,
`src/precis/data/pcb_capabilities.json`, `padplace.py`/`landpattern.py`
(rotation unification), and `handlers/pcb.py::_drc_geometry` (courtyard
tuples for `courtyard_overlap`). `cost.py` reads
`DEFAULT_COURTYARD_RADIUS_MM` — check it is not disturbed.

Touches the placement engine, so `op='place'` and `op='route'` results move
for every board. `tests/test_pcb_reference_end_to_end.py`'s
`BASELINE_DRC_ERRORS` and the 40mm fab-render fixture will both shift.

## Open questions / decisions log

- **DECIDED (user, 2026-08-29): go full arbitrary/convex shape, not a
  bigger circle.** Radii are not a source of truth. Rotation is free via
  matrices at any angle — an earlier note claiming rotation becomes
  expensive conflated compute cost with search dynamics and was wrong; the
  only residue is that anneal parameters were tuned when ROTATE could not
  affect legality.
- **OPEN — does the 40mm reference fixture still place?** Chunky parts
  currently *under*-reserve, so honest polygons need more room. If it stops
  placing, that is a true finding (those parts do not fit on 40mm with
  printable courtyards), but it must be measured up front, not discovered
  late. Decide then whether the fixture grows or the item narrows.
- **OPEN — real values for the two new capability fields.** Must be looked
  up per process, not invented. JLC-typical silk-to-mask ≈ 0.15mm and
  expansion ≈ 0.05mm are the starting point, not the answer.
- **OPEN — keep a broad phase at all?** Recommend deriving the bounding
  radius from the polygon and keeping it only if profiling asks. Adding it
  speculatively reintroduces exactly the two-definitions problem this item
  exists to remove.
