---
status: in-progress
title: "pcb: support-passive groups — netlist-derived organised rows in placement"
prio: normal
pillar: 3d-design
---

# pcb placement: organise support passives into rows

Reto 2026-10-14: "placement is key — if we have a thing with lots of
pull-ups, massive reduction from organizing nicely."

## Problem

Small passives are independent bodies in the anneal: every R/C/D is its
own movable instance, and nothing in the cost model prefers a bank of
pull-ups sharing one rail line, a per-IC decoupling row, or an LED +
resistor pair in a line. On taz15 the human placed the six I2C pull-ups
as three adjacent SCL+SDA pairs at three bus stops and the five MAX3232
charge-pump/bypass caps as a 4-wide row beside U9 plus C17; the anneal
has no way to discover either layout. Each scattered passive lengthens
the rail-side ratsnest, multiplies crossings, and spends router effort a
single shared trace or pour would not.

## What a support group is (netlist-derived, never authored)

Detected from the graph alone (`precis.pcb.support_groups`), then
re-laid as one organised row by a POST-ANNEAL pass
(`OptimizeEngine.organize_support_groups`) — poses only, nothing
stamped, nothing persisted. **Why post-anneal and not a derived rigid
`inst_group` through the search:** measured on taz15-blank seed 1, rigid
rows stamped pre-engine cost the anneal 3.4x its settled objective,
+51% crossings and -1 routed net — a multi-member rigid body is nearly
immovable on a dense board, so a row stamped anywhere suboptimal stays
there. Organising after the search keeps every member's mobility and
ends organised regardless. A polish anneal after the pass was also
measured and rejected (movable-rigid, frozen-rows, and cold t0=0.5
variants all lost ratsnest or routed nets on taz15 seeds 1-2 — the
disturbance is already priced into the pass).

Three shapes:

- **rail_bank** — 2-pin resistors sharing one net on one side and going
  to distinct IC pins on the other (pull-up/pull-down banks). Members
  union when their far nets' served-instance sets overlap at
  `min(2, |A|, |B|)` — identical sets merge (SCL/SDA pull-ups become one
  bank), a single shared hub IC alone does not. The bank then
  SUB-CLUSTERS by each member's nearest served pin (stops within 8mm
  merge): taz15's six pull-ups become three SCL+SDA pairs, one at each
  bus stop — matching the human layout, not one long row mid-bus.
- **support_cap** — 2-pin caps serving one IC: flying caps (BOTH nets
  exclusive to that IC — charge pumps), caps on a fanout-2 net to one
  IC pin (reservoir, dedicated bypass), rail/return-bridging caps
  assigned to the nearest IC pin on the smaller-fanout net. Grouped per
  (IC, package edge), ordered along the edge by served-pin position,
  split on gaps >7mm and into balanced runs of <=4 members.
- **series_pair** — two 2-pin passives sharing exactly one net (a
  fanout<=4 link): R+R series, LED+R, RC snubber. Sharing TWO nets is a
  parallel RC, excluded.

Frozen parts are never members (`fixed`, `max_travel_mm`, existing
`inst_group`, edge-mated, side-mismatched); the pass additionally skips
steering hard-`align` operands. Members must share a board side.

## Layout

Row along the group-local X at a uniform pitch (max member keep-out
diameter + 0.6mm). Per-member rotation puts the SHARED-side pad (rail
pin for banks, return pin for caps, link pads facing for series pairs)
on the row's +Y line — one straight trace serves the row. The row
anchors at the centroid of the pins it serves, nudged a member-radius
+0.4mm off a sole served instance so it lands *beside* the pins;
anchor rotations tried best-cost first, then a 4mm offset ring, all
through `_placement_is_legal` (whole-row proposals including
proposals-vs-proposals). A group with no legal landing keeps its
annealed poses.

## Surface

Opt-in param `groups` (bool) on `op='place'` and `op='route'`, mapped to
`OptimizeConfig.support_groups`; off is bit-for-bit the old run. The
route loop applies the same pass after each round's anneal so every
round's route measures an organised board. The job summary lists moved
groups as `kind: refdes,…` lines.

## Measurement

Same optimize+realize pipeline, same seeds, flag off vs on (taz15-blank
is a prod read-only export; esp32c3 is the repo fixture; iters 2000 /
800; 2026-10-14):

| board | seed | groups | ratsnest mm | crossings | routed | vias |
|---|---|---|---|---|---|---|
| taz15-blank | 1 | off | 10050.4 | 344 | 65/79 | 161 |
| taz15-blank | 1 | on | 9879.0 | 370 | 65/79 | 155 |
| taz15-blank | 2 | off | 10128.9 | 437 | 66/79 | 180 |
| taz15-blank | 2 | on | 10053.4 | 402 | 64/79 | 165 |
| taz15-blank | 3 | off | 10056.4 | 433 | 64/79 | 154 |
| taz15-blank | 3 | on | 9959.4 | 436 | 64/79 | 145 |
| esp32c3 | 1 | off | 1109.7 | 2 | 11/11 | 27 |
| esp32c3 | 1 | on | 793.1 | 48 | 11/11 | 21 |
| esp32c3 | 2 | off | 1096.6 | 7 | 11/11 | 24 |
| esp32c3 | 2 | on | 1029.4 | 65 | 11/11 | 24 |
| esp32c3 | 3 | off | 1128.3 | 2 | 11/11 | 16 |
| esp32c3 | 3 | on | 935.8 | 28 | 11/11 | 15 |
| esp32c3 | 4 | off | 892.2 | 17 | 11/11 | 22 |
| esp32c3 | 4 | on | 974.2 | 28 | 11/11 | 22 |
| esp32c3 | 5 | off | 1156.4 | 8 | 11/11 | 26 |
| esp32c3 | 5 | on | 861.2 | 54 | 11/11 | 19 |

Read: ratsnest improved in 7 of 8 runs (taz15 -0.7%…-1.7%, esp32c3
-6%…-28%, one +9% seed); vias improved or equal in 7 of 8; routed
parity except taz15 seed 2 (-2, the one case where the organised rows
cost the router); crossings mixed (-8% to +15% on taz15; consistently
up on esp32c3 whose baseline is near-zero and hypersensitive). Groups
that cannot be placed legally keep their annealed poses, so the
downside is bounded — the flag defaults off.

Detected and moved on taz15-blank (seed varies; seed 3 shown):
`series_pair: C4,R4` · `series_pair: R3,R8` · `support_cap: C18,C19` ·
`support_cap: C6,C7` · `rail_bank: R11,R7,R12` (SCL+SDA pair at one
stop) · `rail_bank: R15,R27` · `rail_bank: R6,R2` ·
`rail_bank: R22,R23`. Additional series pairs (D3+R29, D4+R31, R21+R37,
D8+R1, R33+R35, R34+R39, D6+R5, L3+R19, D5+L2, C22+R28), the U9 cap row
{C17,C14,C16,C13}, and the remaining bus-stop pairs appear at other
seeds.

Detected on esp32c3: `series_pair: D1,R4` + `D2,R5` (the two LED
pairs), 3-4 `support_cap` rows on U1/U2, and a `rail_bank` of R1-R3 on
VCC3V3.
