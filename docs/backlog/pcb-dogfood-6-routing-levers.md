---
status: ready
title: "pcb: template plaza-row exits for four failed replay nets"
pillar: 3d-design
prio: high
---

# Plaza-row exits after distance assignment and rotation

Reto approved distance warm start → driver rotation → lane template.
Distance landed3419720b6; fixture-only rotation completed with no accepted
pose change. **td472840** owns the remaining row-exit work.

## Rotation result

Six replay arms at3419720b6, exact stored origin
(-0.003060111454470882,0.7518640351003611), bottom side, unchanged other
poses/fabric/rules/grid, distance assignment after pose, default12passes,
negotiation off. **mx458–mx463** record the observations; invalid counts
are diagnostic, not legal gains.

| Rotation | B.Cu routed/55 | In2.Cu+B.Cu routed/55 | Placement/routed errors | Distance mm | Vias B/inner |
|---|---:|---:|---:|---:|---:|
|270°|31|51|0/0|399.24755|0/25|
|0°|21|30|30/30|444.53452|0/10|
|180°|22|30|22/22|372.97075|0/12|

0° causes15 fixed-via/pad contacts;180° causes11. Each contact triggers
clearance and via-pad keepout errors, including foreign or unused HVOUT
pads. The shorter180° assignment is invalid. Preserve270° and the55
existing channels; opening9unused channels needs a generator/IR contract.
The brief's nominal(0,0.75) origin is rounded; use the exact fixture pose.

## Next — connected lane-template probe

Legal inner baseline fails **ARR1_R1C5, ARR1_R6C5, ARR1_R7C2,
ARR1_R7C6**. For these nets, draft short connected In2.Cu runouts from
their existing fixed-island vias toward side corridors. Original through
vias already span In2: a source climb needs no new via. The template owns
actual copper; disconnected waypoint origins are invalid.

Use tests/fixtures/pcb/ewod-dogfood-6-replay-v1.json.gz via the existing
board-snapshot replay hydrate helper in the test DB. Retain original
source fabric and all non-driver geometry,270° bottom pose, class
clearance/width, grid and budgets. Add only explicitly planned connected
copper to an ephemeral replay list. Validate placement/copper DRC before
routing and full routed DRC afterward. Compare one default hard realize
against51/55, retain exact failed nets/vias/track lengths, and link results
to td472840. B.Cu31/55 remains an independent source control.

Existing EscapeGraph supplies pad shells/gap capacity, not a per-net
planner. Island-terminal and maze extra-terminal APIs already accept
connected track endpoints. Prior B.Cu breakout stubs were removed for
congestion; preserve that refused alternative. No new router or global
grid/clearance change. Lane capacity estimates are bounds, not55/55 proof.

## Boundaries

- Never route/modify real.epro2 boards or make fixtures dogfood/look items.
  EasyEDA, gr467885 migration and deployment remain excluded.
- Nano docs fold landed6657e92be separately; original branch deletion Reto.
- No repeat of negotiation sweeps that plateaued at51/55 or B.Cu31/55.
- Local failed-net pair swap remains a future repair; full re-solving
  previously displaced23nets and lost yield.

## Reproduce

Fresh hydrate per arm. Physical rotation uses ir.move_instance; resolve
pin-swap groups afterward, then apply distance warm start. Open In2.Cu
role=signal and class escape layers=[In2.Cu,B.Cu]. Use placement_drc_findings
and routed_drc_findings with actual footprints/fixed copper and voltage
rules; reject placement errors. Rotation raw receipts live in owned
driver-rotation .scratch/rotation.local with hashes in mx458–mx463.
