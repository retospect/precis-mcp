---
status: open
prio: medium
---

# A pad on an obliquely-rotated instance is an axis-aligned rect everywhere but the router

`realize.pad_board_wh` now gives the occupancy grid and the gerber/DRC
model ONE answer for a rect/obround pad's board-space width and height, so
the 90°/270° case agrees. The oblique case does not, and the two sides
disagree in opposite directions:

- **`_pad_shape` (the grid's claim)** falls back to a conservative
  enclosing circle at `hypot(w, h)` for a non-90°-multiple rotation — the
  same fallback `padplace.place_footprint_pads`'s aperture-less writer
  takes.
- **`pads_for_ir` (the model the DRC, the gerber fallback, the fab preview
  and `connectivity` all read)** emits an AXIS-ALIGNED rect at the pad's
  unrotated `w` x `h`, which is not the pad's outline at any oblique
  angle.

**This cannot leak copper into a pad**: the grid's circle strictly
contains the true rotated rect, so anything the router permits clears the
real land. What it can do is make every consumer of the model describe the
wrong copper — a false `clearance` finding against a corner the real pad
does not have, a fab preview whose pad is visibly mis-drawn, and a
`connectivity` pad-touch test that answers off the wrong outline.

The fix is a true rotated polygon, not a swap: emit `shape: "polygon"`
with the four rotated corners for a rect/obround pad whose total rotation
is not a 90°-multiple, so `drc._copper_item_polygon`'s existing polygon
branch measures the real land. `PadGeom.axis_aligned` already carries
exactly the "is this pad's w/h trustworthy as a board-space rect" fact the
decision needs, and `_transform_local_point` already does mirror-then-
rotate for a single point.

Reachable today only through an authored/imported footprint with an
oblique instance or per-pad `rot` — the placer only ever emits 90°
multiples, which is why the 90°/270° half of this bug was the one that
actually fired. An `.epro2` import (`pcb-easyeda-round-trip`) is the
likely first real source, so sequence with that thread rather than
against it.

## Acceptance

- A rect pad on an instance at, say, 30° appears in `pads_for_ir`'s output
  as a polygon whose vertices are the rotated corners, and
  `drc._copper_item_polygon` returns that outline.
- The grid's claim still CONTAINS the model's pad at that angle (the
  over-claim is allowed to stay; a claim narrower than the pad is the
  thing that must never happen).
- A test that fails if the model's pad is the unrotated rect — not merely
  one that passes on an axis-aligned fixture, which is vacuous here.
