---
status: draft
title: "pcb: preserve routed neighbours during local escape repair"
pillar: 3d-design
prio: high
---

# Local escape repair after the routing-levers probes

**td472840** owns the remaining four-net congestion. This is a follow-up
spec for review; further repair implementation is not authorized by the
completed distance → rotation → lane-template ruling.

## Shipped and measured

Distance warm start landed on main (full CI green): Hungarian
actual-via/channel Manhattan assignment defaults before route, with
explicit radial control. Replay inner 51/55 versus 42/55; B.Cu 31
versus 22. mx456/mx457 record the source experiment.

Rotation mx458–mx463: 270° gives B.Cu 31/55 and inner 51/55 with zero
placement and routed-copper errors. 0° has 30 errors and 180° has 22;
both are refused against the immutable fixed vias. Keep the exact driver
origin (-0.00306, 0.75186 mm), 270°, bottom=true.

The four-net connected In2 lane pilot **mx464** is legal but regresses to
50/55 from 51/55. It recovers R1C5/R7C2 and loses R2C5/R5C7/R7C0;
R6C5/R7C6 still fail. No lane-template default or pose change ships.
All names below carry ARR1_ prefixes.

| Net | Exact via x,y mm | In2 endpoint x,y mm |
|---|---|---|
|R1C5|1.8625447686219223,-5.625|3.375,-5.625|
|R6C5|1.5955000000000001,7.4045|3.375,7.4045|
|R7C2|-4.887455231378078,7.875|-3.375,7.875|
|R7C6|7.137455231378078,7.875|4.5,7.875|

Each is one horizontal 0.15 mm track on In2.Cu connected to the exact
existing through-via. Four tracks total 7.44 mm; router tracks 446.09 mm,
combined 453.53 mm, 24 new vias.
Placement and post-route copper DRC report0errors. Original fabric,
class/rules/grid/default12passes/negotiation0 and all poses preserved.
A preliminary outer-corridor L-plan failed clearance precheck for R1C5
and was never routed. Legal local clearances do not establish better
whole-board routed yield; reject the measured short template.

Bounded removal controls also earn no default change (mx465–mx467):

| Short exits | Routed/55 | New vias | Placement/post-copper errors |
|---|---:|---:|---:|
|R1C5 only|51|25|0/0|
|R7C2 only|49|24|0/0|
|R1C5+R7C2|49|24|0/0|

R1C5 alone retains the baseline four failures. Both49-net arms recover
R1C5/R6C5, lose R0C5/R3C5/R6C6/R6C7 and still fail R7C2/R7C6.
Same pose/fabric/rules/grid/pass12 controls; all three probes execute
successfully. Isolated clearance and shortened source islands are
insufficient; preserve the51-net baseline and refuse all tested defaults.
Exact derived/added lengths and artifact hashes are in td472840.

## Proposed next repair — review first

Retain the accepted baseline failures R1C5,R6C5,R7C2,R7C6, rather than
treating the pilot's displaced set as the new design. Investigate a local
channel-pair or corridor repair that preserves already-routed neighbours.
Full re-solving previously displaced23nets; connected runouts now recover
two and displace three. Freeze unaffected routed neighbours; a candidate must lose none of the
baseline51 nets outside its explicitly named local repair pair, keep
global routed count≥51, show exact gained/lost nets and pass both DRC
stages.55/55 is the target,
not a claim. Bound iterations explicitly before implementation; no new
router, finer global grid, clearance relaxation or budget inflation.

Use only tests/fixtures/pcb/ewod-dogfood-6-replay-v1.json.gz hydrated via
board snapshot in the test DB. Preserve55channels; opening9unused
HVOUTs requires a separate generator/IR contract. Do not revive the
previously refused B.Cu breakout stubs. Existing island-terminal APIs
accept connected fixed-track endpoints; disconnected waypoint origins
are invalid. Real boards, fixture dogfood/look items, EasyEDA, gr467885,
providers, deployment and Nano circuit design remain excluded.

## Reproduce the refused lane pilot

At main3419720b6 with the current replay hydrate helper, apply distance
warm start, set In2.Cu role=signal/routable and class escape layers to
In2.Cu+B.Cu. Append the table's four fixed track rows to an ephemeral
copy of authored copper; segments are lines from via to endpoint and
width_mm=0.15. Run placement_drc_findings, default realize, then
routed_drc_findings with actual footprints and net voltage/rule inputs.
Source parent3419720b6, probe9345159600 (PCB routing code unchanged).
Raw test/log/exit and JSON receipts are in the owned driver-rotation
worktree's tests/rotation.local and .scratch/rotation.local; exact roots
and hashes in td472840. lane-short-result.json SHA256:
abd6d3d35398c20144afb44c3472aed5cfb2b6f4b68dff656df683f0bd5c24c6.
These are internal asserted replay measures, not paper-verified evidence
or runtime/deployment proof. Nano docs independently landed6657e92be;
Reto owns original branch deletion.
