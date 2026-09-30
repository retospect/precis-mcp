---
status: ready
prio: high
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

## Second incident, same day — this one cost a full root-cause pass

`ewod-dogfood-6`, 2026-09-30. Reto saw pads and vias overlapping on the
rendered board. `view='drc'` reported 116 errors whose rows read:

```
error clearance via[ARR1_R3C3] <-> pad[ARR1_R7C0] on B.Cu -0.090  copper clearance 0.000mm
```

Read literally that says "the R3C3 plaza via is touching the R7C0
electrode" — two cells at opposite ends of an 8x8 grid, at exactly
0.000 mm. It sent this session hunting a coordinate/name-join corruption in
the pad pipeline, and it sent a root-cause pass after a rim/corner
via-math defect. **Both were wrong, and the message is why.**

`pad[ARR1_R7C0]` is not the electrode. It is `ARR1_SINK_0.HVOUT24` — a
bottom-side pad on the driver IC's real manufacturer footprint — which
carries the net name `ARR1_R7C0` because `pcb_pin_swaps` bound that channel
to that electrode's escape net (`session.apply_pin_swap_overrides`).
Verified directly against prod, scoped to the design: `HVOUT24 ->
ARR1_R7C0`, `HVOUT23 -> ARR1_R6C0`, `HVOUT15 -> ARR1_R1C0`. So the "far
apart grid indices" were never grid indices, and the exact 0.000 mm was a
real via sitting in a real solder land.

This is the **same trap gr451052 already diagnosed and fixed for
`check_via_pad_keepout`**, whose code now carries the comment: *"reporting
only the net invites reading `pad[ARR1_R3C4]` as 'the R3C4 electrode' when
it is the driver IC's own land ... that misreading cost a whole
investigation."* The sibling rule `check_clearance` never got the
equivalent fix, so the identical misreading happened again — and because an
escape net's two members sit on OPPOSITE layers and sides (electrode body
on F.Cu/top, driver channel pad on B.Cu/bottom), a net name is maximally
ambiguous exactly here.

**Raised to high.** This is no longer "locating a violation is tedious": a
net-only label actively produced two wrong diagnoses of a fabrication-fatal
defect, on a board whose real problem
(`pcb-placer-obstacle-set-is-mounting-holes-only.md`) was already filed and
ranked. One glance at `refdes/pin` would have gone straight there.

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
