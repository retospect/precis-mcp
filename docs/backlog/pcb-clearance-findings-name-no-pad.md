---
status: ready
prio: medium
pillar: 3d-design
---

# A `clearance` finding says which nets are too close, but not which pad or where

`drc.check_clearance`'s findings carry net, ctype and layer:

```
error  clearance  track[GND] <-> pad[RXD] on F.Cu  -0.050  copper clearance 0.040mm < JLC min 0.090mm (4layer)
```

On a board with four RXD pads that names no pad and no coordinate, so
locating the violation means re-deriving the geometry by hand.
`check_via_pad_keepout` already solved this (gr451052): its findings name
the pad's refdes/pin. `pads_for_ir` puts `refdes` and `pin` on every pad
dict it emits, and both features' coordinates are in the items
`clearance_pairs_indexed` already holds, so this is reporting, not new
geometry.

**Measured cost of not having it:** the 2026-09-30 investigation behind
`realize.pad_board_wh` spent an afternoon identifying a single offending
pad (J2.RXD) that the finding could have named outright, and the whole
diagnosis turned on the pad's ORIENTATION — which a finding carrying the
pad's box would have shown at a glance.

## Acceptance

- A `clearance` finding involving a pad names its `refdes`/`pin`, and both
  features carry a coordinate (the nearest points of the two shapes, which
  shapely's `distance` pass already computes internally).
- Findings stay ordered by model index (the property
  `test_check_via_pad_keepout_reports_pads_in_model_order` pins for the
  keep-out rule) so the report is stable across runs.
- No measurable cost added to the pass. `check_clearance` is already the
  geometric DRC's bottleneck — 195 ms of a 287 ms pass on an 8x8 EWOD tile,
  780 ms of 1202 ms at 16x16, measured after
  `check_via_pad_keepout` was indexed — so this must not add a second
  distance computation per pair.

test: `tests/test_pcb_drc.py`
