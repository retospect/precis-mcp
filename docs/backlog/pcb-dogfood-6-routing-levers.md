---
status: ready
title: "pcb: replay driver rotation, then plan jammed-row escapes"
pillar: 3d-design
prio: high
---

# Routing levers remaining: driver rotation and jammed-row escapes

Reto authorized distance-assignment warm start, then driver rotation, then
lane template on2026-10-07. Warm start is implemented: owning PCB docstring and
`precis-pcb-route-help` carry its contract. **td472840** owns the remaining
work; **mx456/mx457** record the fixed-pose replay's 51/55 versus 42/55
on In2.Cu+B.Cu (31/55 versus22/55 on B.Cu). No real-board proof.

## Slice 2 — driver rotation (after slice 1 lands)

Use `tests/fixtures/pcb/ewod-dogfood-6-replay-v1.json.gz` via
`tests/test_pcb_escape_replay._hydrate` in the test DB. Keep fixed
copper, all non-driver poses, origin (0,0.75), bottom side, class
clearance/width and route passes. Resolve the same distance assignment
after each candidate rotation: current270°, 0° and180° (±90°). Use
In2.Cu signal and escape layers `[In2.Cu,B.Cu]`; the B.Cu lock is a
separate control. One hard `realize()` per arm; no negotiation sweep.
Measure routed count, exact failed nets, vias, assignment distance and
geometric DRC. Record results in precis linked to td472840; update
`threads/ewod-pcb.md` at this rank. Only change routing behavior if a
fixture-backed improvement supports a separately reviewed contract.

The ring interior is21×15mm against a14.7×14.7mm plaza field; current
270° jams the top/bottom rows against pad rows carrying39channels. A
90° rotation jams the left/right columns carrying16/0channels instead.
Translations and a wire-only side flip do not test that hypothesis.

## Slice 3 — lane template (after rotation)

Measure a per-via corridor plan for the13top and21bottom jammed-row vias:
drop onto an aligned channel pad when possible; otherwise climb to
In2.Cu and exit through a side corridor. Middle-row21vias can climb and
reach the left column or nearest corridor. The ledger/template owns the
plan; no new router or global grid/clearance changes. Lane estimates
(60vertical/36horizontal per signal layer at0.249mm pitch) are capacity
bounds, not proof of55/55. The legal dogfood-1 best remains50/54; the old
54/54 arm routed across forbidden fabric/F.Cu and cannot be cited.

## Boundaries and open decisions

- Never route or modify Reto's real.epro2 boards; replay fixtures never
  become dogfood/look items. EasyEDA, gr467885 migration are excluded. Nano
  readiness docs fold is separately authorized.
- Existing55used HV507channels only. Exposing9unused channels needs a
  generator/IR contract; not authorized by this slice.
- Failed-net repair remains open: a local pair swap with reroute of two
  nets, rather than a full re-solve that displaced23nets and lost yield.
- Negotiation plateaued on the distance start (51/55 at10and50iterations)
  and on B.Cu (31/55 at10); do not repeat those sweeps.

## Probe method

Hydrate the snapshot, apply route overrides as the existing replay does,
set physical rotation with `ir.move_instance(driver, rot=angle)`, resolve groups
with `pcb_route._resolve_pin_swap_groups`, and apply
`pcb_route._apply_pin_swap_warm_start` after the pose. Open In2.Cu in
`ir.stackup` and `class_rules["ewod_ARR1_escape"]["layers"]`, then
`realize.realize(ir, config, footprints, fixed_copper)`. Use
`session.routed_drc_findings` with actual footprints and fixed copper;
count unique failed net names, not merely the number of segments.
