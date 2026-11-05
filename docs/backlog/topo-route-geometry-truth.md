---
status: draft
title: "topo route — geometry truth: why taz-leaf does not route at any board size, and the slices that complete it"
pillar: 3d-design
prio: high
model: opus
---

# topo route — geometry truth

Filed 2026-11-01 (Devin builder, taz-leaf) after the 2× board
(`taz-leaf-x2-local-2`: 6/38 nets, 3.7× the area) routed no better than
the 1× board. Supersedes the D-b/D-c slices of
[topo-route-review](topo-route-review.md); its A/K/C/B1 slices and
evidence stand. Rulings in force: no grid router, any-angle wires with
few segments, a GND pour on In1 as an interim, a decap or crystal beyond
its limit fails DRC.

## Bottom line

The geometry-truth half is landed (G4, G1, G3, G2a–c, G2e, G2f): 0 foreign pad hits, 0 voided via sites, 0 sharp turns, emitted length 1.01 × raw taut, every failure named. The invariant is not met. Near-zero rules: the 2× pose routes 30/30 in four of five net orders and 29 in the fifth; the 1× poses route 27/29 (`local-66`) and 28/30 (`local-61`); 5, 5 and 3 GND pads have no via site. Real rules at 2×: 16/29 one pass, 13 no-via branches, 5 crossings. Two failure classes remain. (1) Via room: a hop is sited on the finished wire inside the interval its crossings and masks leave, while the search's fit (`wires._via_fit_base`) passes an edge with 0.01 mm outside the pad covers and sees no wires, vias or label boxes. Every one of the 14 failed cuts at real rules has 0 legal points in its interval; 11 are hops within 0.13–0.79 mm of a 0.5 mm-pitch pad or an 0402; at near-zero the same mechanism leaves 3 no-via branches on `local-66`, one with a zero-width interval. (2) Crossings no hop resolves: inside a via-barred keepout (`OK_HYST`×`OK_PROG` on `local-61`, `VBAT_OV`×`VRDIV` at 2×) and at the U1/U2 pad rows at real rules. These need a planar detour or the earlier net moved; G2e's wall rounds fire 0 times at 2× in production order. Order: G6 (measure every board) → G2g (plant vias in the search) → G2h (edges split at earlier chords, one bounded negotiation) → G2d (GND out of via-barred zones with the same planter) → G5a (one route pass) → G3b (fan before spread). Of the 13 real-rule failures at production order about 10 are the router's; `ANT` and `S1_A` are class-bound (their classes say F.Cu, their candidates are layers 0, 2, 3), `S2_REF` and the GND pads in the antenna and comb keepouts are the pose's. (The route-stage via has since been decided and landed: c5011fe32/af0b29a66, `_via_choice_rules`, the fab-floor 0.45/0.2 for every net at or under one barrel's current; every real-rule gauge row since shows `via {'0.45': n}`. Measured against the 0.75 house via in `scratch/review/via-current-report.md`: gauge x2 +0..+2, sanity +2..+4, local-66 flat, local-61 −2..−6 — the fit minting hops the planter later made plantable; product path x2 23 → 29/38, locals flat.)

## History

The dossier that opened this file (27 taut crossings, 2 detected; 94 foreign-pad violations; `_spoke_offsets` manufacturing 123 sharp turns; the plane never reaching the DB path; 20 re-route passes) is in 742d8ca12 and its probes in `scratch/review/probe_*.py`. Two findings stand: 24 of 50 parts carry synthesized footprints (G0 gate, Reto's bindings via `precis-part-select`), and provenance exists on the builder side only ([who-made-you-provenance](who-made-you-provenance.md); slice 2 *moved by* gates G5b).

## Status

| slice | state | numbers (2× pose, production order unless named) |
|---|---|---|
| G6 gauge | built (uncommitted); `scratch/review/g6_table.py` runs sanity, `local-66`, `local-61`, 2× at near-zero and real rules in five orders, `rules_t` carries the via; `bench.py` prints the one-pass row; strict = `_route_pass` through its guards, unreachable nets count | `tests/test_pcb_topoplace_invariant.py`: 20 rows, 2 pass (sanity prod, shuf2), 18 strict xfail with failing nets |
| G0 footprints | open | synthesized 24; route numbers provisional |
| G4 plane_net → `pcb_planes` | landed e82120062 | stored boards still need the re-put |
| G1 geometry-true crossings | landed 75cff850c | 26/27 detected, 1 cancelled bigon, 0 false |
| G3 one kink pass | landed 46de841dd | sharp turns 0, length 1.01×; 3-wire slit xfail open |
| G2a/b truthful walls, vias stay | landed 6c91cb082 | pad hits 0, voided sites 0 |
| G2c layer state in the search | landed 5bf9cf43f | via cost 1/3/6 give one row |
| G2e earlier nets close layers | landed acf2540f5 | wall rounds fire 0× at HEAD; 3× on `local-61` without resolving its crossing |
| G2f centreline in free space | landed f45befb3f | near-zero 30/29/30/30/30; real 16/15/16/17/19 of 29; `bench_x2` 16/20/18/15/19 |
| HEAD, one pass (G6 table, strict, production order; spread over the five orders in brackets) | — | near-zero: sanity 20/20 [17–20]; `local-66` 24/30 [24–26], 2 no-via, GND missing 5; `local-61` 28/30 [25–28], 1 live + 1 planned crossing, GND 3; 2× 30/30 [29–30], 52 vias, GND 5. Real, the search fitting the 0.75 via: sanity 16/20 [14–18]; `local-66` 14/30 [13–16], 13 no-via, GND 7; `local-61` 17/30 [11–18], 12 no-via, GND 5; 2× 15/30 [14–16], 11 no-via (13 with the 0.6 search via), live 4 + planned 1, 30 vias, GND 7. Strict counts the nets `_route_pass`'s guards withdraw (`guarded`, `severed`) and unreachable nets; without them (nets with a defective branch only) `local-66` is 27/29, 2× real 16/29. `_Built.dead` is 0 in all 40 rows |
| via-current | landed c5011fe32 + af0b29a66 | signal via = fab floor 0.45/0.2 by net current; numbers in Bottom line |
| G2g planted vias | in the coordinator branch (G2h built on it) | near-zero strict, base → tree, prod/rev/shuf1–3: sanity 20/17/17/20/17 → 20/20/18/18/16; `local-66` 24/24/26/24/24 → 25/27/27/27/27; `local-61` 28/27/25/27/28 → 25/28/28/25/25; 2× 30/29/30/30/30 → 30/30/30/28/30; no-via 0 on all 20 rows |
| G2h sub-interval state + negotiation | built on `worktree-topo-g2h` (head 73b9d8303), merged into the coordinator branch; commit 3 (`OCC_LAYER_COST`/`TRI_OCC_MM` A/B) open | near-zero strict, base 10c8d6d32 → HEAD, prod/rev/shuf1–3: sanity 20/20/18/18/16 → 20/20/20/20/20; `local-66` 25/27/27/27/27 → 27/27/27/27/27; `local-61` 25/28/28/25/25 → 30/30/30/30/24; 2× 30/30/30/28/30 → 30/30/30/30/30. Real 2×: 14/13/15/13/14 → 22/13/18/17/17 of 30, live + planned crossings at prod 12+5 → 2+2 |
| G2d GND out of via-barred zones | in flight `worktree-topo-g2d`, round 5 (`scratch/review/topo-g2d-report.md` there) | strict identical to base in all 40 cells; GND missing summed over 5 orders, base → r5: local-61 real 20 → 3, local-66 30 → 18, x2 30 → 14; near-zero 15/25/25 → 5/20/15 (total 145 → 75) |
| S1 honest crossing failure + `drc_local` | in flight `worktree-topo-shove-s1`, measured, uncommitted (`g6_s1_{base,after}_*.json` there) | real strict summed: sanity 76 → 81, local-66 88 → 97, local-61 81 → 98, x2 86 → 100 (331 → 376, gate was 356); near-zero 100/135/144/150 → 100/135/149/150 (local-61 shuf3 24 → 29); `drc_local` 0 on every real row both sides; live+planned crossings 0+0 on 16 of 20 real rows (base: every row); x2 real `wall_s` prod/rev 19.8/59.1 → 13.6/18.0 s |
| G5a / G5b one route pass | open; G5b after who-made-you slice 2 | 8–10 passes today |
| G3b fan before spread | open, new | 4 vias over 3 passes on the shared-corner test |

The HEAD row above is the 2026-11-02 table; current numbers are in
"Current issues" and the S1 row.

## Current issues (2026-11-03, coordinator HEAD 31e5da71e)

Landed since the table above: the 0.20 mm via-to-pad mask web enforced
in search and emit (G2h follow-on), vendor silk with a ring fallback,
and the tier-2 goldens. Real-rule strict-clean summed over the five
orders: sanity 76/100, `local-66` 88/150, `local-61` 81/150, 2× 86/150
(331/550). Near-zero: 100/100, 135/150, 144/150, 150/150. Uploaded
boards `-1103b`: 1× 26/31 nets, 2× 27/31.

- **Most real-rule failures are mutual trace blocking.** Of 219 failed
  instances, 146 route alone, and the first blocker is always an
  earlier net's trace (never a stub, via or plane net). That is slice S,
  [topo-route-shove](topo-route-shove.md).
- **The 0.20 mm web causes 53 failures**, and changing it moves 47
  others. Via siting is globally sensitive. That is V's share, after S.
- **Same-net via collisions: 8.** G2i (join same-net copper) covers 7
  of them, so it is a subset of V.
- **Four nets are walled off on every layer**: `DEC1`, `VBAT_OV` and
  `VSTOR` on `local-66` by the PI-stiffener hanger-ring keepout, and
  `S2_REF` (R13) on 2× by the edge, parts, a hole and three keepouts.
  On `local-66` the same keepout traps the GND pads of C11, C13 and R23.
  These are placement or keepout facts, not routing: slice P below.
- **S1 is measured** (row in Status): +45 summed at real rules against
  a +25 gate, no board or near-zero row below base, `drc_local` 0 on
  every real row. The honest failure also empties `plan.crossings` on
  16 of 20 real rows and takes x2 rev's wall from 59 to 18 s — the
  adopted probes were what `assign_layers` spent its time on. The
  one-swap oracle (+48) was measured at base, before S1; the S2 gate
  below is re-derived from it, not inherited.
- **G2d round 5 is measured** (row in Status). Left at real rules, all
  cap-independent: C15, R23, R25 on 2× (19–24 mm escapes), C11, C13,
  R23 on `local-66` (trapped, slice P), SNS2's comb (30–54 mm), and
  U1's thermal pad on all three boards — `g2d_rung_dbg2.py` vetoes
  all 128 ring candidates BEFORE any routing (pads, label boxes, the
  web), so the thermal pad reaches GND only through via-in-pad
  (decision below).
- **Via room is the 0.45/0.2 fab-floor via and its 0.20 mm web**, not
  the 0.75 house via — that choice landed (Bottom line). The web A/B
  (`topo-ab-web-report.md`): removing the web lifts 53 instances and
  creates 47, a net +6 of 550, so tenting is not a lever; re-siting the
  hop along the finished wire (V) is.
- **The 1× pose of criterion 2 is not on the table.** `taz-leaf-t` is
  `local-70` (`scratch/taz-leaf/fable_route_prompt.md`); the table
  routes `local-66` and `local-61`. Criterion 2 is re-pinned to the
  table's poses.
- **Prod shows main's code** (silk, DRC) until the next `/go`.

## Plan

Each slice is one commit with its test, reported on the G6 table
(sanity, taz-leaf 1× at the `taz-leaf-t` pose, 2× at the x2-local-2
pose). Numbers are the 2× pose unless named. Order (2026-11-03): land
G2d and S1 (both measured, independent code) → S2 → S3 → V → G2j →
G2h commit 3 → G5a → G3b → G0 → G5b; P (placement) runs beside them
and gates nothing but criterion 2. Why this order: S2 recovers the
largest class (146 congestion instances) and changes which wires exist,
so V (vias on finished wires) and G2h's price A/B wait for it; G5a's
deletion is safe only once one pass carries the numbers. The via-size
A/B the order once waited on is done (via-current, Bottom line).

**G6. One gauge, every board.** `scratch/review/zr_gauge.py` runs on every board of the table (sanity, 1× `local-66`/`local-61`, 2×) at near-zero and real rules in the five orders, with `rules_t` carrying the via (today the search fits 0.6 while `plan_route` sites 0.75; production uses 0.75 in both). Rows: nets clean, no-via (named reason), live and planned crossings, vias, GND pads without a site, `occupancy_retry`/`occupancy_walled`, search-time dead layerings (`_Built.dead` is read nowhere today), wall. The invariant becomes a test: near-zero 100 % and GND 0 missing on every board in every order, strict-xfail per board until met, the failing nets listed in the reason. Label keepouts and the edge band stay in force at near-zero; the test says so. Accept: the table re-baselined; `bench.py` reports one-pass numbers beside the loop's. Deletes: `g2c_blockers.py`'s own-net blocker count. Tier: coder.

**G0. Library footprints or no score.** The batch put, the bench and
`view='check'` count parts whose footprint is synthesized (no cached
catalog row and no board-local footprint) and name them; the bench
refuses to print a route row above `synthesized: 0`, and `check` reports
each as an `error` (`footprint_synthesized`), a board-local footprint as
`info`. taz-leaf's 24 get LCSC bindings through `precis-part-select`
(Reto's picks; the table of values is in `board.py`), BT1 a datasheet
footprint. Test: a fixture with one unbound 0402 gives one error; bound,
none. Accept: taz-leaf `synthesized: 0`. Tier: coder for the gate,
Reto + `precis-part-select` for the bindings.

**G4. `plane_net` feature → `pcb_planes` on the store path.** In the
batch apply loop (`_pcb_ops.py`, the `for ft in features` block) route
`ftype == 'plane_net'` through `_pcb_assign_plane` — the same write
`op='plane_net'` uses — instead of `_pcb_insert_feature`; one converter
shared with `local._planes_rows`. Re-apply `op='plane_net'` to the
stored taz-leaf boards. Test: a batch put with the feature gives
`ir.net_plane_layers[GND] != 0` in the job's IR and `view='planes'`
lists it. Accept: a job run's `wires.json` has no GND tree; ≈ 30 fanout
vias; the no-plane row of the table disappears. Tier: coder +
test-author; one hour.

Landed e82120062; numbers in Status.

**G1. Geometry-true crossings.** In `assign_layers`, replace the order
inversion with proper intersection of the taut polylines (an STRtree
over branch `points`; the intersection's arc positions on both branches
give the bracketed spans `_pair_regions` returns today). Delete
`_cdirs`, `_pair_sign`, `_flip_is_locked`, `_pair_live`, the order half
of `_pair_regions`/`_crossing_pairs`, and in `wires.py`
`_repair_order` and `order_residual` (`order` itself stays: corridor
capacity and G3's fan ranking read it). Test: two straight wires crossing
in open mesh are one crossing; two wires passing opposite sides of a free
vertex are none. Accept: detected == present (27/27); after
`assign_layers`, same-layer crossings in emitted copper ==
`plan.crossings` (today 23 vs 0). Tier: coder, with the probe as oracle.

Landed 75cff850c; numbers in Status.

**G3. One kink pass on the index.** Keep `_spoke_offsets`' shared-vertex
fan (rank by `order`, offset along the bisector by the slot sum) and
`_emit_shapes`' kink + pull-taut; make `_kink` offset at the offending
point against `CopperIndex` (pads, zones, other pieces at their current
shape), not at the leg's midpoint. Delete the chord, boundary-run and
Powell passes (`cluster_cost`, `_chord`, `_line_ok`, `_seg_seg_dist`,
`_seg_aabb_dist`, `_free_span`, `_seg_box_interval`) and `_pad_polys`
in favour of the index (topo-route-review D-a's remaining half). Test:
a wire grazing one pad gets one kink at the pad; a two-pad diagonal
stays one segment. Accept: sharp turns in emitted copper 0; emitted
length ≤ 1.1 × raw taut; stage ≤ 5 s on 2×. Tier: coder.

Landed 46de841dd; numbers in Status.

**G2. Layer-aware route search.** `wires._net_tree`'s state becomes
(edge, triangle, layer): the per-layer mask `_open_layers` already
computes is used as is instead of collapsed by `!= 0`; a layer change is
a transition with a via cost, allowed only on an edge where a via of the
net's rule fits (the `CopperIndex` query `_site_via` already makes,
precomputed per edge and outer-layer pair). Runs and cuts fall out of
the search, so `_legalise_open`, `_runs_open`, `_run_open` and
`_initial_segment`'s repair go; `assign_layers` keeps only crossing
resolution (G1) over the search's runs. First experiment: via cost = 3 mm
and "fits" = both outer-layer `gap_free_mm` ≥ via_dia + 2·clearance,
measured on the G6 table before any tuning. Test: a two-pad net whose
straight line crosses a foreign 0.4 mm pad row routes around it on one
layer; the same net with a 2 mm free channel beside the row takes two
vias through it. Accept: straight-through-pad-field branches 0; no-via
joins 0 (today 10; 20 without the plane); vias ≤ 49; placed airwire ≤
1.3 × raw taut. Tier: main loop (Opus) for the state/cost design, coder
for the implementation.

**G2a. Truthful walls.** Every pad is a constrained polygon in the mesh on
its own layers: pins from their real pad shape, `pad_extra` lands added,
combs from their polygon. An edge on or inside a foreign pad or a zone is
closed on that polygon's layers, interiors included, replacing the fan
boundary. Frozen fanout stubs are clipped to outside the pad. Accept: 0 pad
hits and 0 runs on a zone-barred layer at vanishing rules; real rules no
worse on the table.

**G2b. Vias stay where they were asked for.** Drop the nearest-edge dedupe;
a hop is sited inside its crossing's bracket or fails with a reason, and the
walk step scales with the via. Accept: no voided site, every crossing event
resolved or named.

Landed 6c91cb082; numbers in Status.

**G2c. Layer state in the search**, as described above, now justified: the
corridor must be chosen knowing where a via fits. The corridor stays the
output (retrack and funnel change edges afterwards), and layers are then
assigned on the final edges by the same rule.

Landed 5bf9cf43f; numbers in Status.

**G2e. Earlier nets close their layers.** Within a pass, each net's search
sees earlier nets' (edge, layer) occupancy as closed on that layer, so a
crossing is decided where fit is known: a via before it or a detour. Runs come
from `layer_dp` after each net's tree. Order-dependent, so measure it. Fallback
if it loses: re-thread one partner away from the pad field (`assign_layers`).

Landed acf2540f5; numbers in Status.

**G2f. A centreline routes in free space.** Gripe 550979: fit and
the open rule are per edge, while the funnel may hug a pad at `trace/2 +
clearance` where a via needs more, and `_pull_taut` then straightens through
pads a piece already skims at 0.007–0.021 mm (`ANT`).

Landed f45befb3f; numbers in Status.

**G2g. Vias are planted in the search.** A layer change in `wires._net_tree` is legal on edge `e` only where a via point exists: a point of `e`'s free stretch — pads at `via/2 + clearance` on every layer, zones barring any layer, the outline margin, label boxes, and the earlier nets' wires and planted vias from `_Occupancy` — and that point is chosen then and fixed. The hop is a vertex: the funnel tautens pad → via and via → pad as two runs, later nets see the via as an obstacle (a disc body in the index and closures on the edges it covers, every layer), and `plan_route` emits what the search planted. No interval is slid afterwards, so a zero-width interval cannot arise; a hop with no plantable point is not a transition, and a pin the layered search then cannot reach is named (`Unreachable`, layered). Accept (G6 table, strict: guards and unreachable nets count): near-zero `local-66` 24 → 30/30 at production order (its 3 unreachable nets DEC1, VBAT_OV, VSTOR and 4 severed, OK_PROG, VBAT_OV, VSTOR, DEC4, included), 2× rev 29 → 30/30, sanity 20/20 in all five orders (today 17 in rev/shuf1/shuf3: LR08–LR10 guarded for touching tracks); real 2× no-via 11 → ≤ 4 at production order; vias no more than HEAD's at 2× production order (52 near-zero, 30 real). Deletes: `realise._site_via` (walk, walk2d, walk_grid), `_hop_events`, `_hop_intervals`, `_windowless_cuts`, `_legalise_open`, `_runs_open`, `_run_open`, `_initial_segment`'s repair, `assign_layers`' flip and `rethread` search (`_MAX_FLIPS`, `_yield_rank`, `_pair_live`), ≈ 900 lines. Decision (Reto): the route-stage signal via — decided and landed as the current-rated fab floor (via-current, Bottom line). Tier: main loop for the planter and funnel seam, coder for the deletions.

Built on `worktree-topo-g2g` through round d (head 50e6b10a5), awaiting land. The re-plan kept the search-time planter and added the post-hoc half back planter-legal: a `layer_dp` re-layering of the just-built branch in `_grow_net` (call site 1, strict-decrease fixpoint, replant on same-net hop collisions) and `assign_layers` re-based on `b.layers` with either-partner moves — 0-via whole-branch flips, bracketed spans, whole-corridor DP excursions, rethread — every cut judged by the planter (call site 2). `plan_route` emits `b.vias` only; `_live_crossings` reads `b.layers`. `_site_via`, the three walks, the hop-interval machinery, `_window_mask`/`via_window` and the nominal legaliser are deleted (`realise.py` ≈ 4510); `layer_dp`, `_runs_open`, `_yield_rank`, `rethread` stayed — the plan's "delete assign_layers' search" became "keep it, planted". Against the accept list: **no-via is 0 on all 20 near-zero rows** — the class is gone by construction, no interval exists to be zero-width; 2× production order is 30/30 including the RF_FEED×VRDIV no-hop knot the same-net replant freed; 2× near-zero vias 38–48 per row under base's 52–57 (36–52 across all 20 rows); x2 `_route_pass` 8–19 s. Open vs the literal targets: sanity is 20/20 in two orders, not five — shuf1–3 carry `guarded` µm-overlap pairs the taut resolved (emitted-copper level, same class the old ledger named); `local-66` is 25–27, not 30 — the DEC1/VBAT_OV/VSTOR unreachable-and-severed trio persists unchanged; `local-61` lost prod/shuf2/shuf3 to live-crossing severances the merge introduced (25/25/25 vs base 28/27/28); 2× shuf2 is 28 (S1_A×S2_REF live pair). Every residue row is a live-crossing or emitted-level guard pair — the earlier-partner negotiation the pass cannot make, i.e. the G2h class the re-plan predicted — not interval siting. Round d's 2×2 on the six breaching rows (`_PORTAL_EDGE_MM` = ∞, `TRI_OCC_MM` = 0, both, neither — `scratch/review/g2g_d_ab.json`) restored none of them as a set: portal-off lifts only the two sanity guarded pairs and worsens `local-61` shuf3 and 2× shuf2, tri-occ-off lifts only `local-61` shuf2 while via counts explode — the breach is the G2h class, not the portal/tri-price interaction. Real-rule rows were not re-tabled this round.

**G2h. Edges split at earlier chords, one negotiation.** State (edge, interval, layer): each edge is split at the earlier same-layer wires' crossing points, a step is legal only when both ends lie on the same side of every earlier chord in the triangle, so the search sees a crossing where it would be made and can pass on the chord's other side. When the later net has no planar way and no hop (both wires F.Cu-only in a via-barred zone, or a pad row), the earlier net is re-grown once with the later wire walled and kept if its key does not worsen; otherwise the crossing is named. Accept (strict): near-zero `local-61` 28 → 30/30 at production order (OK_HYST and OK_PROG are severed after the crossing), 2× 30/30 in all five orders (rev 29: XL1 severed); real 2× live crossings 4 → ≤ 1 and `plan.crossings` 1 → 0 at production order. Deletes: G2e's wall rounds (`_WALL_ROUNDS`, `_Occupancy.wall`, `occupancy_walled`), the "alone" retry, `OCC_LAYER_COST` pricing if the table shows it carries nothing. Tier: main loop.

Built on `worktree-topo-g2h` (head 73b9d8303) with three review rounds. `_Occupancy.add` records each earlier wire as per-triangle chords and edge splits (end-point splits and sub-nm chords skipped; a chord lying along a mesh edge is recorded under both triangles and a step endpoint on a chord's interior is a crossing); `_net_tree` searches (edge, interval, layer); `_taut_pts` clips portals to the chosen intervals; one negotiation re-grows the blocking earlier net, kept only if `(comps, on-layer cross)` does not worsen. Deleted: `_WALL_ROUNDS`, `_Occupancy.wall`, the alone retry. Also: `_swap_bigon` swaps only when both arcs are on one layer; when pass 2 strands a survivor the stage keeps pass 1 minus the failed nets (`_strip_failed`) and still runs the re-route rounds; a refused negotiation's demand is recorded as a `refused` corridor cut and reported, and `CorridorOverflow` is raised on routed demand only (coordinator ruling: a real board must come out, unfittable nets named). Against the accept list: `local-61` production order 30/30, 2× 30/30 in all five orders — met; real 2× production 2 live + 2 planned crossings — not met (≤ 1 / 0). Residue, next slices: `local-61` shuf3 24 (base 25; a re-rolled crossing cluster around RF_FEED/OK_HYST/OK_PROG/XC1/XC2/RF2); the fan_slit squeeze through a vertex where two earlier chords meet is not expressible (2-via hop accepted; an on-vertex state is G2i material); `plan.overflow` has no consumer yet. `topo_synth` is a timing/determinism fixture (its nets share pins by construction), not a routability invariant: 58–83/180 at default rules after G2h, which refuses the crossings base crammed. From Reto's x1 review (2026-11-02), next: G2i Steiner attach (a branch may join same-net copper on another layer — VRDIV double via) and G2j needless hop pairs (S1_A).

G2h follow-on (built on `worktree-topo-via-mask`, Reto ruling 2026-11-03): vias are not tented, so a via keeps a soldermask web — `via_mask_web_mm`, pad mask expansion + dam + via mask expansion, 0.20 mm at the JLC-min figures — off EVERY pad's copper edge, own-net pads included; the planter's own-pad copper-overlap exemption is gone (`_via_fit_base`, `candidates`, `static_survivors`, `_fanout_vias`, the emit guard and `drc.check_via_mask_web` all enforce it, the last net-blind with the same fixed-via electrode carve-out as `via_pad_keepout`). Search and emit share one pad predicate: `_ViaPlanter`'s pad index is `_guarded_branches`' roster and world geometry (`realise._pad_polys` bodies tagged `clearance=cp`), so a foreign pad bars `max(cv, cp, web)` at both stages and an own pad `max(cv, web)` at search — the emit guard's own bar is the web alone, so the `cv > web` corner is stricter at search, never a disagreement. Same-net fixed copper stays exempt; `plan_route`'s corridor-less naive mint (fixture meshes only) is the one path outside the predicate.

**G2d. Plane pads a zone leaves no hop for.** A plane-net SMD pad with no fanout site (4 in the chip-antenna keepout L1–3: C15, R23, R25, R28; 1 under the comb: SNS2) becomes a two-pin routed branch from the pad to the nearest point G2g's planter accepts outside the via-barred zones, on the pad's layer. The U1 and U2 thermal pads (every candidate within `_FANOUT_RADII_MM` ≤ 2.0 lies on the own pad) are a decision: via-in-pad for pads wider than the fanout reach, or radii that scale with the pad. Failing a site, the pad is a named finding. Accept (re-pinned 2026-11-03 on round 5's measurement): signals identical to base in every cell of the table; escape copper adds 0 `drc_local` errors; escapes ≤ 10 mm off the pad copper; GND missing on every board and order is exactly the named set — `local-66` C11/C13/R23 (P), 2× C15/R23/R25 (19–24 mm), SNS2 (comb) and U1's thermal pad until via-in-pad is ruled — nothing else. Round 5 meets this: GND missing summed 145 → 75 with that set as the whole residue. Tier: coder.

**S. Trace shove** — [topo-route-shove](topo-route-shove.md) (S1 measured, S2, S3). The S1 result above also meets G2h's open accept line (real 2× live crossings ≤ 1, `plan.crossings` 0 at production order).

**V. Vias re-sited on the finished wire.** The 53 web-caused instances (`topo-ab-web-report.md`) and the 8 same-net via collisions (`planted vias collide (same net)`, the only `no_via` reason left at HEAD): after `build_wires`, a hop whose planted point fails the emit predicate is slid along its own branch inside the stretch the branch's chords and the web leave, or the branch is re-layered by `layer_dp` with that stretch closed; a same-net collision is resolved by joining the branch to the colliding same-net copper (G2i Steiner attach, VRDIV's double via) before re-siting. Measured first on S2's table: the web A/B is re-run at S2 HEAD, and V is sized to what remains web-caused. Gate: `no_via` 0 on every real row; real-rule sum ≥ S2's; `drc_local` ≤ S2's per board. Tier: main loop for the predicate seam, coder for G2i.

**G2j. Needless hop pairs.** A branch that hops to a layer and back with no crossing between the hops (S1_A on 1×, Reto's x1 review 2026-11-02) is flattened by `layer_dp` with a via price that makes the pair strictly worse than the flat run. Gate: vias on the table ≤ V's row for row, strict unchanged. Tier: coder.

**P. Placement walls.** The table's four walled nets (`local-66` DEC1/VBAT_OV/VSTOR under the PI-stiffener hanger-ring keepout; 2× S2_REF at R13) and the three trapped GND pads (`local-66` C11/C13/R23) are pose or keepout facts: no slice above can move them, and criterion 2 is unreachable on `local-66` while they stand (ceiling 27/31). Ruled (b), automatic (Reto 2026-11-03): a placer term or check, "every pad has a route on some layer, and every plane pad has a via site". Hand-editing the poses was option (a) and is not the fix. Gate: corridor-limit instances 20 → 0 and trapped GND pads 3 → 0 on the re-baselined table. Tier: main loop designs the placer term; a coder builds it.

Readiness work can run beside S2: [placement access diagnostics](topo-placement-access.md)
and [thermal array geometry/process prerequisites](topo-thermal-via-array.md).
Their prototypes do not change production placement, routing or fabrication.

**G5a. One route pass.** Delete `_route_signals` passes 2..N, `_merge_pass`, `_copper_zones`, `_PARTIAL_MAX_SEGMENTS`, `_REROUTE_ROUNDS`; `RoutePlan`'s failure fields collapse to one `failed: {branch: reason}`. None of this moves a part. Accept: `bench_x2` complete within 1 of the loop's row on each order at ≤ 1/5 the wall; `route_design` ≤ 30 s at 2×. Decision (Reto): releasing this half from the slice-2 gate; the ruling names G5 as one slice. Tier: coder.

**G5b. Route stage moves nothing.** In `pcb_place.py` the shove loop, `_route_failure_pushes`, `_bbox_gap`, `_nearest_pin_part`. After who-made-you slice 2. Accept: every part's move log shows no route-stage entry; hard-measure errors after route == after `settle_measures`.

**G3b. Fan before spread.** In `_spoke_offsets` the shared-edge spread runs before the bend fan, so a wire whose neighbours were already spread cannot be lifted without a hairpin (`test_spoke_offsets_fan_shared_gap_and_shared_corner`: 4 vias over 3 passes, 0 before G2f). Run the fan first, or spread about the fanned corner. Accept: that test plans 0 vias; `test_slit_wires_all_realize[3wires]` flips or keeps its xfail with the measured reason. Tier: coder.

## Who builds what

Each slice names its tier. Sonnet `coder` agents build; a Fable `reviewer`
reviews each slice before it is committed; the main loop designs G2g's planter
and G2h's state. G2g, G2h, G2d and G3b all touch `realise.py`/`wires.py` and
run in series; G6 (scratch gauges and one test) can run beside any of them.

## What this deletes

`realise.py` 4939 → ≈ 2000: G2g takes `_site_via` and its three walks, the hop intervals, the legaliser and `assign_layers`' search (≈ 900); G5a the recovery loop (≈ 350); G3b nothing. `wires.py` loses G2e's wall rounds (G2h). Kept: `TopoDesign`, the CDT mesh with grown rings, the Prim tree with its (edge, interval, layer) state, corridor capacity, `CopperIndex`, `crossings.py`, `layer_dp`, pin assignment, labels, `widen_power`, fanout and pour, `wires.json`, masks, fab overlays.

## Explicitly NOT in scope

A grid or maze router (ruled out 2026-11-01); a new placer (the revisit
clause fired — the table shows walled nets — and is slice P, a pose or
keepout change by hand before any placer term); blind/buried vias; the DB side of
provenance beyond sequencing slice 2 before G5b; choosing the 24
C-numbers (Reto, `precis-part-select`); the EasyEDA round-trip.

## Acceptance criteria

1. Near-zero rules: every signal net clean and no plane pad without a via site, on every board of the table in every one of the five orders, as a test — the walled nets and trapped pads of slice P excepted by name until P lands, and SNS2's comb and U1's thermal pad until the via-in-pad ruling (today: `local-66` 27/30 in every order, `local-61` 149/150, 2× and sanity 100 %).
2. taz-leaf 1× (`local-61` and `local-66`, the table's poses — `taz-leaf-t` is `local-70` and is not gauged) and 2× (x2-local-2 pose), production order, one pass: ≥ 28 of 31 real nets complete, committed copper DRC 0, hard-measure errors unchanged by the route stage, `synthesized: 0`. Ceilings today: `local-66` 27 (P), 2× 29 (S2_REF + GND's 19–24 mm pads), `local-61` 31. Evidence for reach: after S1 the production-order rows are 22/30, 22/30 and 23/30 signals; the one-swap oracle says 57 % of the remaining congestion nets route when pinned, 18 % with their blocker; S2 + V have to lift production order by 5–6 nets, which the oracle's per-cell best (sanity 8, `local-66` 10, `local-61` 12, 2× 18 summed over five orders) supports on `local-61` and 2× and not on `local-66` without P.
3. Sanity 20/20, DRC 0 (pcb-topo-place acceptance 9).
4. Geometric crossings detected == present, and same-layer crossings in
   emitted copper == `plan.crossings`, on every board of the table.
5. Emitted copper: 0 sharp turns; length ≤ 1.1 × raw taut.
6. `route_design` ≤ 5 min on 2× at every order (Reto 2026-11-03: "a
   working result beats a failure that is quick"). Optimise anyway, and
   report wall time on every row.
7. `realise.py` ≤ 2000 lines; `RoutePlan` has one failure field; one
   clearance oracle on the route path (`CopperIndex`).

## Risks

- Every real-rule row before G6 and via-current is provisional (the search fitted 0.6 against a 0.75 plan; the signal via is 0.45 since c5011fe32). Via room at the fab floor is now a fixed fact — the 0.20 mm web is worth a net +6 of 550 (web A/B) — so what remains at fine-pitch parts is siting (V) and placement (P), not a rule choice.
- The gauge read plan pieces only until S1: G2d round 1 passed the strict table with 3/6/6 clearance errors from escape copper the gauge never saw. `drc_local` must run over everything `route_design` emits (signals, fanout stubs and vias, escapes, rungs), i.e. after `_emit_escapes`, on every slice's table — a column over `_route_pass`'s result alone repeats the blind spot once G2d lands.
- G2g changes the search's cost surface; measure vias and airwire ratio on the table before any tuning.
- G4 changes what the stored taz-leaf boards mean: every revision carries
  the feature, so applying it re-plans GND on all of them — lands with a
  note on the thread, not silently.
- G0 blocks the route score on 24 part bindings that only Reto makes;
  until then the table carries `synthesized: 24` and the route numbers
  are provisional.

## Open questions / decisions log

Decided: the route-stage signal via (fab floor by current, landed);
G2h's one bounded re-grow (built; S extends it to rounds, ruled
2026-11-03).

Ruled 2026-11-03 (Reto):
- **Thermal via-in-pad: yes**, for heat transfer more than for current
  ("We want it for heat transfer, moreso than current"). This is a new
  slice, **G2t**, after G2d: every exposed thermal pad (U1, U2) gets a
  via array inside the pad to the GND plane, sized and pitched for
  thermal conduction. That means as many fab-floor or larger barrels as
  the pad holds at the via-to-via rule, not one via. It is a named
  thermal fanout kind, and it is independent of whether G2d's escape
  also reaches the pad. Before building, record the fab's via-in-pad
  option (plugged/capped vs open), since open vias wick solder. Gate:
  every thermal pad has its array on every board, 0 new `drc_local`,
  signals unchanged.
- **Slice P: option (b), automatic** ("It ought to be automatic"). The
  placer must not leave a pad without a route on some layer, nor a
  plane pad without a via site. It needs a placer term or check, so that
  the walled nets and trapped pads come out of placement, not by
  hand-editing the poses. Owner: main loop design, then a coder.
- **Criterion 6:** ≤ 5 min (above).
- **The shove budget and variant search:** see
  [topo-route-shove](topo-route-shove.md) "Decisions".
- **Merge S1** into the coordinator branch once its round-2 review
  passes, before S2.

Decisions for Reto:
- A layer force for the 'rf' and 'sense' classes (their notes say F.Cu, `net_layers` gives [0, 2, 3]).
- G5a ahead of who-made-you slice 2.
- G0's `BT1`: a datasheet footprint under `footprints` (board-local,
  `info`) or a JLC consignment row — Reto.
- Keep the In1 GND pour after G2, or route GND on In1 as topo-route-review
  C first intended? The G6 table answers it: if GND routed on In1 reaches
  DRC 0 the pour interim ends (D2).
