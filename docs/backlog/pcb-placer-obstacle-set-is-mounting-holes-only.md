---
status: draft
title: The placer sees mounting holes and authored vias — authored tracks, pours and the seed are still blind
prio: normal
pillar: 3d-design
---

# What the placer still cannot see

The two defects this item was filed for are fixed. `courtyard_overlap` and
`_placement_is_legal` are side-aware (2026-09-29), and authored vias are
placement obstacles (2026-10-01): `PcbIR.fixed_vias` carries every
`pcb_fixed_copper` via, `OptimizeEngine._fixed_via_gap` measures each of a
part's real solder lands against them, and a move that puts a land within
the router's clearance of a via is illegal. The regression test is
`tests/test_pcb_ewod_dogfood.py::test_dogfood_place_keeps_every_land_off_the_authored_plaza_vias`
— authored copper in the model, a real `op='place'`, measured net-blind.
It was red at 4 lands on vias before the fix.

What is left is narrower, and none of it is known to bite a live board.

## Motivation / why

The obstacle set is still partial. Each gap below is the same shape as the
one just closed: geometry that exists before placement runs and exerts no
force on where a part lands.

### 1. Authored tracks and pours are not obstacles

`session.fixed_vias_from_copper` keeps `ctype='via'` rows only. An authored
TRACK on an outer layer is real copper a land of a foreign net must not sit
on; nothing stops it. It has not bitten because the one generator that
authors copper (`ewod_pad_array`) puts its tracks on F.Cu under its own
locked array's courtyard, where no same-side part can land anyway, and its
B.Cu rows are via flashes only. The next generator that authors a B.Cu
track, or an `.epro2` import with fixed copper, reopens it.

A track obstacle is layer-specific where a via is not, so it needs the
land's SIDE, and a through-hole land collides on every layer.

### 2. The seed is still blind

`seed_placement` avoids neither holes nor vias (its docstring says why for
holes). Legality gates PROPOSALS, never the incumbent, so a part seeded on
a via stays until a proposal walks it off; the graded `~fixed_via` margin
entry gives it a slope. On the dogfood board the anneal leaves at the
default 2000 iterations. A short run may not, and then
`OptimizeResult.on_fixed_vias` names the parts and the `pcb_place` job
summary says the placement is not routable — reported, not prevented.

`pcb_route` re-places through the same engine and does NOT surface
`on_fixed_vias` anywhere; `pcb-always-valid-board-invariant.md` owns
refusing to route an illegal placement, which is the right home for it.

### 3. Lands are axis-aligned rects in the footprint frame

`session.land_rects_by_instance` covers an obliquely rotated pad with a
square of its longer side and a polygon pad with its bounding box. Both
over-cover, so they can only reject a legal position, never admit an
illegal one. (`pads_for_ir` now emits an oblique pad as its true rotated
polygon; this placer keep-out box is still the conservative cover.)

### 4. A rigid recentre is vetoed outright

`recentre_in_outline` now returns `(0, 0)` on any board with authored
vias, the same blanket rule a locked instance already triggers. Correct,
and coarser than it needs to be.

## In scope

Authored tracks (and pours, if a generator ever emits one) as
side-specific land obstacles, in `_fixed_via_gap`'s sibling. Seed-time
avoidance only if a board is measured that the anneal cannot rescue.

## Explicitly NOT in scope

- The refuse-before-route gate
  (`pcb-placement-must-be-valid-before-routing`, folded into
  `pcb-always-valid-board-invariant.md`).
- The corridor-width term (`pcb-placer-starves-the-escape-corridor`).
- Via SPAN correctness (`pcb-via-geometry-ignores-pad-side-and-pads`). The
  placer treats every authored via as a through hole on purpose.
- `ftype='keepout'` features (`pcb-keepout-does-not-bind.md`) — same
  defect class, different geometry source; whoever does either reads both.

## Target + blast radius

`src/precis/pcb/session.py` (`fixed_vias_from_copper`,
`land_rects_by_instance`), `src/precis/pcb/optimize.py`
(`OptimizeEngine._fixed_via_gap`, `_placement_is_legal`,
`_refresh_courtyard`). Moves placement on every board with authored copper.
