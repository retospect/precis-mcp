---
status: evidence
title: Global tile-capacity plan probe on the EWOD dogfood-6 replay
pillar: 3d-design
prio: high
---

# Global tile-capacity plan probe on the EWOD dogfood-6 replay

**Question (experiment, not a feature):** does a capacity-feasible 55/55
routing plan exist for the `ewod_ARR1_escape` class (In2.Cu + B.Cu), and
does handing that plan to the maze router as corridors beat the 51/55 of
`test_connected_via_channel_assignment_replay`'s `("distance", True)`
arm? Code: `src/precis/pcb/globalplan.py` (pure functions), driver
`tests/test_pcb_globalplan_ewod.py` (slow). Nothing is wired into jobs,
handlers or defaults.

Measured on the hydrated replay fixture
(`tests/fixtures/pcb/ewod-dogfood-6-replay-v1.json.gz`), same static
copper the maze stamps (captured `_route_pass` arguments, then
`_claim_static` on a fresh grid — pads, fixed copper, mounting holes,
fiducial keep-outs). Resolved rules the router actually uses: track
0.15 mm, class clearance 0.099 mm, via diameter 0.75 mm (the task's
0.45 mm figure is the plaza vias, not the routed via rule). Maze grid
401x314 cells at pitch 0.0675 mm → 55x43 tiles of 0.5 mm over 2 planned
layers.

## Part 1 — capacity maps

Edge capacity = per contiguous free run of each tile boundary,
`floor(run_mm / (track + clearance))`; free = no cell of foreign copper
dilated by the track keep-out radius (half width + 1 cell slack, the
router's own `r_cells`). Claims owned by the 55 participant nets are
exempt from the track mask so each net can leave its endpoint tiles —
this is the model's main overestimate (plaza-via keep-outs between
neighbouring escape nets read as free lanes). Via capacity = greedy
count of 0.849 mm-spaced via envelopes on cells via-legal on ALL four
layers (via keep-out disk + pad keep-out mask); participants NOT exempt.

Edge-capacity histogram (edge count by lane capacity):

```
        cap=0  cap=1  cap=2
x edges   333   2471   1840
y edges   347   2538   1735
via tiles 1873    492      —   (envelope 0.849 mm > tile, never ≥2)
```

Per-row summary (x/y edge sums and minima, via sites per row):

```
layer 2 (In2.Cu): interior rows cap_x sum 49–108, min 1–2; cap_y min 0
  on every row (pad-row keep-outs dilate whole y-boundaries); via sites
  per row 3–17 through the array, ~55 on the two border rows.
layer 3 (B.Cu):   cap_x sum 38–108, min 0 on most interior rows (the
  HVOUT pad row walls); cap_y sum 38–76, min 0 everywhere; via sites
  per row 3–17, 54–55 on border rows.
```

Full per-row table is printed as `capacity_map` in the test's report.

## Part 2 — global plans (PathFinder on the tile graph)

Present cost `base * (1 + 0.5 * over)` growing ×1.5 per iteration plus
history 1.0/unit; reroute conflicted nets only; deterministic.

| variant                                  | converged | iters | trail                        | tile len | vias | time  |
|------------------------------------------|-----------|-------|------------------------------|----------|------|-------|
| stored pin swaps                         | yes       | 8     | 21,14,8,3,3,2,2,0            | 458.0 mm | 7    | 0.49s |
| distance warm start                      | yes       | 7     | 19,10,7,3,3,2,0              | 423.0 mm | 11   | 0.35s |
| negotiated (free sinks + Hungarian/iter) | yes       | 9     | 43,34,26,19,10,9,4,2,0       | 419.0 mm | 10   | 2.71s |

Zero over-capacity resources at plan end in all three variants. Counting
only resources USED to their limit (cap > 0; a cap-0 wall nobody uses is
not a saturated cut — an earlier draft of this note counted those and
read "~3000 saturated"), 502 / 548 / 443 edges or via tiles sit at
capacity for stored / distance / negotiated, out of ~9700 with cap > 0.
Which cuts saturate (tile rows, 0.5 mm from the board origin; layer 2 =
In2.Cu, layer 3 = B.Cu; count = saturated edges on that row):

```
stored:      x B.Cu row 23 (22), x B.Cu rows 32–33 (16–17), x In2 row 35 (16),
             x B.Cu row 10 (15), y B.Cu rows 4–6 (14–15 each)
distance:    x In2 row 35 (34), x In2 rows 20–23 (22–23), y B.Cu rows 4–6 (15–16)
negotiated:  x In2 rows 34–35 (34 each), x In2 row 20 (22), x B.Cu row 10 (19)
```

The same two features bind in every variant: the horizontal lanes across
the driver's pad rows on B.Cu (rows 10, 21–23, 32–33 — the HVOUT pad-row
walls) and the y lanes at the array's bottom edge on B.Cu (rows 4–6); the
distance and negotiated assignments shift load onto In2.Cu rows 20–23 and
34–35 instead. A capacity-feasible 55/55 plan EXISTS on this tile model for
every assignment tried.

## Part 3 — corridor-guided detailed route

Best plan = `stored` (fewest vias). Corridor = plan tiles dilated by d
tiles, most-constrained-first order, then the ordinary failures-first
leftover passes with corridor results held as verbatim `preferred`
proposals. First measured by stamping CONTESTED outside the corridor
(which also forbade in-corridor vias); re-measured the same day with a
real per-cell track mask on the search (`OccupancyGrid.route(mask=)`,
via disks free to overhang):

| dilation | restriction      | routed inside corridor | exhausted | final after leftovers |
|----------|------------------|------------------------|-----------|-----------------------|
| d=1      | CONTESTED stamps | 14/55                  | 0         | 38/55                 |
| d=2      | CONTESTED stamps | 18/55                  | 0         | 42/55                 |
| d=1      | `mask=`          | 15/55                  | 0         | 38/55                 |
| d=2      | `mask=`          | 19/55                  | 0         | 41/55                 |

d=1 final unrouted: R0C5 R0C7 R2C4 R2C7 R3C2 R3C3 R3C4 R3C6 R4C5 R5C3
R5C4 R5C7 R6C2 R6C4 R6C7 R7C5 R7C6. Total experiment ~50 s.

## What this says

At 0.5 mm tile granularity a feasible 55/55 plan exists under all three
pin assignments on THIS tile model — which overestimates capacity
(participant keep-outs read as free lanes; a through-via is not charged
against the other layers' track lanes), so "feasible" is an upper
bound, not a proof that the sequential router's failure is the missing
plan. The corridor hand-off LOST to the plain router (38–42 vs 51) under
BOTH restrictions. The first explanation — CONTESTED walls forbidding
in-corridor vias — is refuted: a real track mask with the via disk free
to overhang routed 15/19 inside the corridors instead of 14/18, final
38/41. So the corridors themselves are wrong at the detailed level: the
tile model's capacities read lanes the fine grid does not have
(participant keep-outs count as free; a through-via is not charged on
the other layers' lanes), the plan commits nets to those lanes, and the
confined search fails where the unconfined one finds another way. The
honest reading: "a feasible plan exists on the tile model: yes";
"hard corridors from that model help: no — they hurt". Next experiment
(slice 0b, not built): (a) make the tile capacities faithful (charge
participant keep-outs and through-vias) and re-plan; (b) corridors as a
COST, not a wall — the existing `maze.Negotiation` history term with
usage zero prices off-corridor cells (step cost × (1 + k)) with no new
router code; measure both on the replay and on the plaza member of
`tests/fixtures/pcb/qfp_fanout.py` (53/55).
