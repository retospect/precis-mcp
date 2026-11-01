---
status: draft
title: "topo route review: why taz-leaf does not route, the fix order, and what to delete"
pillar: 3d-design
prio: high
model: opus
---

# topo route review — why taz-leaf does not route

Design review of the topo place+route path against
[pcb-topo-place](pcb-topo-place.md), measured on taz-leaf at the
2026-10-31 pose (`taz-leaf-local-66`, seed 1, preset `flow`, effort 40000;
the placement is byte-identical for local-62..66) and at the board
script's authored seed pose. Filed 2026-11-01 (Devin builder, taz-leaf).

## Bottom line

The router is solving a different board from the one taz-leaf needs, and
when it fails it throws the copper away instead of repairing it.

1. **The placement input is wrong.** The topo job never passes the 13
   authored `near` measures (decaps ≤ 2 mm, X1 ≤ 3 mm) to the anneal.
   The `flow` preset prices wirelength at 4 %. So the anneal triples the
   airwire (264 → 765 mm) to remove 6 straight-line crossings, and puts
   crystals, the RF match and decaps 17–24 mm from their pins.
2. **The routed netlist is wrong.** Flex-slot pin assignments are never
   written into the netlist the router sees, so 10 MCU connections are
   never attempted. "16 realized" is 5 complete nets, 4 slot nets missing
   their MCU pin and 7 nets with nothing to route.
3. **The rails share the signal layers.** GND (36 pins) and VSTOR
   (12 pins) are routed as taut trees on F/B. In a compact field every
   short signal crossing them needs a layer flip, and the flip needs a via
   that has no site. At the authored seed pose, 21 of 23 failures are
   `no via site`, including 2 mm neighbour nets (SDA, SCL, S2_DRV,
   XC1/XC2). In1 and In2 carry no committed copper.
4. **The realiser filters instead of solving.** Copper is built
   topologically, then turned into geometry by heuristics. It is judged by
   at least five clearance models that do not agree with each other or
   with the DRC. Each veto withdraws a branch or a whole net, and recovery
   means re-running the whole route (pass 2, re-route rounds, the job's
   failure shove). `realise.py` is 4951 lines and `_spoke_offsets` alone is
   1289. 32 commits since 2026-10-25 each added a special case tuned to the
   one pose every experiment reused.

Fix order: make the problem true (measures, slot write-back, honest
counts, containment, decap/crystal distance as a DRC error), emit the topo
wires at any angle with few segments (Reto 2026-11-01), add a multi-pose
bench, move GND off the signal layers, and then make the realiser repair a
failed kink inside its corridor against one clearance oracle instead of
withdrawing the net. The any-angle emit and the repair delete most of
`realise.py`.

**Rulings 2026-11-01 (Reto):** the grid router stays retired — the model
is topo, then kink the wires; wire angles are free ("I don't care about
0,90,45 degree wires. Just make it work any way with few segments now");
a GND pour on In1 is allowed as an interim if it is really needed; a
decap or crystal beyond its limit fails DRC.

## Evidence

All numbers are from `taz-leaf-local-66` (job 227; `summary.txt`,
`check.txt` and `wires.json` under `scratch/taz-leaf/out/local/`) unless
marked as seed.

| measure | value |
|---|---|
| nets | 38: 31 with ≥2 routable members, 7 with <2 (all reported `realized`) |
| complete nets | **5/31** (RF1, XL1, DEC5, LBST, VREF_SAMP); SDA, SCL, S1_M and S2_REF are realized without their MCU slot pin |
| airwire | 264 mm seed → 765 mm placed (same 2–2.9× growth on every run since local-44) |
| objective line | `crossings 0.30 / ratsnest 0.04 / … / measures 0.00` |
| nets with a pin-to-pin tree edge > 10 mm | 18, of which 17 failed; nets with every edge ≤ 10 mm: 8/12 realized |
| worst offenders | XC1/XC2 17 mm (32 MHz crystal), ANT/RF_FEED/RF2 18–24 mm, DEC4 19 mm, XL2 18 mm, U2 dividers 12–22 mm |
| parts pushed off the leaf outline | C11, C13, R23 during the settle: legalize pushed them off, pulled them back on far from U1 (C11 at x = 17 mm, U1 at x = 112 mm); DEC1 fails "walled off" |
| committed copper | F.Cu 27 tracks / 140 mm, B.Cu 3 / 33 mm, In1/In2 none, 6 vias |
| failure digest | 23× no via site, 16× short-risk, 9× unconnected, 4× same-layer crossing, 4× unrouted, 3× non-octilinear |
| seed pose, same router | 8/31 real nets realized; 23 failed, 21 with `no via site` (SDA R30–U3, SCL, S2_DRV R12–R13, XC1/XC2 X1–U1 among them) |
| sanity board (spec acceptance 9: 100 %, DRC 0) | 15/20, 22 vias, DRC 0 since 186d60098 |
| job wall 427 s | anneal `ProxyState.try_move` 264 s (label room `_mg_label` 108 s); `route_design` 136 s (`_site_via` 93 s, of which `walk_grid` 72 s); `_spoke_offsets` 26 s |

The seed row matters. Reverting to the compact authored pose does not
route either, so problems 1 and 3 have to be fixed together: a compact,
electrically correct pose has to be routable.

## Spec vs built

| spec piece | state |
|---|---|
| Phase 0 proxy placer, pair map, pin assignment | built; measures dropped on the job path; the assignment is rendered into summary and `pins.h` but not applied to the routed netlist (Decision 14) |
| Decision 10 cost ladder (route the elites) | `Ladder` hook only; no rung is registered anywhere in `src/`, so the placer never sees a routing result |
| Phase 1 crossing graph, colouring, certificates, via bracket | colouring is `assign_layers`' local search; certificates and the bracket print `n/a (slice 2)` |
| Phase 2 topology anneal | not built |
| Phase 3 corridor capacity, LP shove | built (`wires._corridor`, `shove.shove_pose`); outline containment is one courtyard-in-polygon rule since 2026-11-01 (`topoplace/contain.py`) |
| Phase 4 realiser | built; acceptance 9 not met |
| me507871 widen at the end | built (`widen_power`, with the DRC as its oracle) |

## Problems, ranked by effect on routing

### 1. The routed problem is not the board's problem

- `src/precis/workers/job_types/pcb_place.py::_topo_run` calls
  `design_from_ir(..., measures=graph.get("measures"))`. Measures live on
  `design.measures`, which is what the legacy path in the same module
  resolves (`resolve_measures(design.measures, …)`). `td.measures` comes
  out empty, so the `measures=0.2` override never fires.
- `_topo_run` computes `assignment = _tp.assign_pins(...)` and only
  writes it to run files. `_topo_route` builds `td_route` from an `ir`
  whose `pin_net` does not have the slot pins. The check confirms this
  with `unconnected_pin` ("10 pad(s) on U1 netless but assigned to topo
  flex slots").
- `sketch_rows` reports a net with <2 members as `realized`. The headline
  count is inflated by 7, and partially wired slot nets count as complete.
- Containment had three rules (fixed 2026-11-01, A4). `placer.py::topo_place`
  and `shove.py::shove_pose` clamped to `_legal_xy` over the outline's
  **bounding box**, and the leaf outline is concave; only
  `legalize.py::legalize_pose` tested the polygon. On taz-leaf it was
  legalize's own pushes that moved C11 and C13 off the board, and its
  pull-back that parked them 95 mm from U1.
- `flow` gives ratsnest 0.05 against crossings 0.40, silkroom 0.20 and
  margin 0.15. Straight-airwire crossings are a 2-layer planarity proxy.
  On 4 layers a crossing costs at most two vias, but a 20 mm detour fails
  the net, and for a crystal or RF match it fails the board electrically.
  A precedent exists: the pcb-platform thread records an "import
  incumbent" ruling for the deleted field placer (reject re-placement
  that is more than 2 % worse in wirelength than a legal input pose).

### 2. Rails compete with signals on F/B

GND's 36-pin tree and VSTOR's 12-pin tree are taut polylines through
every cluster. Any short net they cross has to flip a span to another
layer, which mints two vias. In a compact 0402 field those vias have no
site. me507871 already says every copper layer is a routing layer, but
`assign_layers` treats the inner layers only as places to send a
crossing loser. Nothing reserves a layer for a rail, so In1/In2 end up
empty.

### 3. The realiser filters instead of repairing

Pipeline per pass: `build_mesh` → `build_wires` (homotopy and capacity)
→ `_spoke_offsets` (fan out shared vertices, boundary-run moves, chord
shortcut, Powell relaxation) → `plan_route` (`assign_layers`, then
`_site_via` with four walk strategies) → seven vetoes → `realize_result`
(octilinear L/jog/simplify redraw). The seven vetoes are: strip
`crossings` branches, `_guarded_branches`, `_severed_nets`, the
off-angle refusal, `no_via`, `unreachable`, and the partial-copper filter.

- Geometry is decided after topology, by heuristics that can leave the
  corridor the capacity check priced. An L corner or an axis jog can enter
  foreign space, and the guard then withdraws the copper.
- A veto never feeds back locally. Recovery is a whole re-route: pass 2
  for survivors, then up to 8 re-route rounds against frozen copper. The
  frozen copper is rebuilt into the CDT each round, and a
  `MeshConflictError` ends the rounds. After that the job's failure
  shove runs up to 3 more full `route_design` calls.
- The shape the guard judges and the shape that gets emitted have drifted
  apart before (the simplifier comment above `_SIMPLIFY_ENABLED` still
  says OFF while the value is `True`).

### 4. Five clearance models, one truth

Pad geometry on the route path is built by `wires.guard_pad_rects`,
`realise._pad_polys`, `realise._pad_clear`, `masks._pad_polys` and
`legalize._pad_polys`, and DRC uses `realize.pads_for_ir` through
`session._copper_model`. Segment distances are re-implemented in
`_seg_dist`, `_seg_seg_dist`, `_seg_aabb_dist`, `_seg_rect_clear`,
`_free_span` and `_seg_box_interval`. Every disagreement shows up as a
withdrawal (router stricter than DRC) or a DRC error (router looser):
gr524392, gr543905, and the 2026-10-31 C10/DEC5 hop.

### 5. Measurement and speed

- Every router change since local-62 was measured on one pose, so fixes
  are tuned to it. The DEC5 hop, the SDA/SCL leg counts and the RF_FEED
  snake are all properties of that pose.
- A loop costs 7 minutes, and the route bench at a fixed pose 70 s. Via
  siting is the route hot spot (93 s), and the 2.5 mm `walk_grid` disc
  added in 9d7122db6 is 72 s of that.
- Tests in `tests/test_pcb_topoplace_realise.py` (3409 lines) mostly pin
  heuristic shapes ("a 3-leg U", "collapses a staircase to one L"). Few
  pin invariants such as "committed copper is DRC-clean" or "a realized
  net joins all its pins". The shape tests have to change whenever the
  heuristics do.

## Plan

Each slice is one commit with its test. Slices A, E1 and C need no new
ruling except C's D2.

**A. Make the problem true**
- A1. Fix the measures argument in `pcb_place.py::_topo_run` (pass
  `design.measures`). Test: a job-path fixture with one `near` measure
  gives a non-empty `td.measures` and a non-zero measures share in
  `summary.txt`.
- A2. Apply the slot assignment to `ir.pin_net` before `_topo_route`
  builds `td_route`, and persist it the way the job persists poses. Test:
  a flex-slot fixture's routed tree contains the MCU pin, and the check
  stops reporting `unconnected_pin` for slot pads.
- A3. Honest status. Nets with <2 members get `empty`, not `realized`.
  The digest and summary report complete nets and connections realized
  out of required (pin pairs). Test on the sanity board and a slot fixture.
- A4. Landed 2026-11-01: one courtyard-in-polygon rule
  (`topoplace/contain.py`) held by the anneal, the shove LP (a cut along
  the crossed edge's normal, then bisection) and legalize. Left: legalize's
  own overlap/zone pushes still move parts off the board and rely on its
  pull-back; scatter and restart jitter still use the bounding box.
- A5. Folded into K (the octilinear simplifier and its flag go).
- A6. Decap and crystal distance fails DRC (Reto 2026-11-01). A violated
  `hard` proximity measure is an `error` finding in `view='check'` and
  `view='drc'` (rule `measure`), naming both parts, the limit and the
  distance; a violated `soft` one is a `warn`. The limit is the measure's
  own goal (decaps 2 mm, X1 3 mm, X2 4 mm in `board.py`), and the decap
  and crystal measures there become `hard: true`. Test: a hard proximity
  C↔U at 2 mm placed 5 mm apart gives one error; placed within 2 mm, none.

**K. Any-angle emit (Reto 2026-11-01).** `realize_result` emits each
piece's spoke-offset polyline as it is — the taut topo wire with its
spacing kinks — then drops a vertex wherever the straight shortcut stays
clear under the same clearance predicate, so pieces carry few segments at
any angle. `_octilinear`, `_axis_jog`, `_octi_links`,
`_simplify_octilinear`, the off-angle refusal and `RoutePlan.off_angle`
go; `_coalesce_micro` loses its 45° condition; `drc.check_octilinear`
leaves the geometric run (the 2026-08-31 house rule it enforced is
superseded). Test: a two-pad diagonal net emits one segment; a net bent
around one pad emits at most three; no `octilinear` finding; sanity and
taz-leaf numbers before and after.

**E1. Multi-pose bench.** A bench script routes 3 poses (legalized seed,
seed 1, seed 2) × 3 boards (sanity, usbc_attiny, taz-leaf) and prints
complete nets, connections, DRC errors, vias, the placed/seed airwire
ratio and wall time. Every later slice reports this table.

**C. GND off the signal layers.** GND gets an exclusive inner layer
(In1) through `net_layer_force`, and every other net's candidate layers
drop In1. Each GND SMD pad gets a fanout via within 1 mm on its mount
layer. GND then routes alone on In1, so it has no crossings, and
`widen_power` fattens it. If routed GND on In1 still fails on the bench,
pour In1 with `planes.plane_pours` instead, keeping the per-pad vias (an
interim, Reto 2026-11-01). VSTOR on In2 is the same move if the bench
shows it binds. Test: at the taz-leaf seed pose no signal net's crossing
or guard record names GND, and committed copper is DRC 0.

**B. Placement objective**
- B1. `flow` gets a real measures share and ratsnest ≥ 0.2. Test: taz-leaf
  decaps and X1/X2 end within their bounds at 3 seeds with no `measure`
  finding.
- B2. Dropped for now: with A1 and A6 the anneal can no longer pull decaps
  and crystals away. Revisit only if E1's placed/seed airwire ratio stays
  high after B1.

**D. One oracle, then repair inside the corridor**
- D-a. `CopperIndex`: per-layer STRtree over DRC-source pads
  (`realize.pads_for_ir`), keepouts, the board-edge band and committed
  copper, answering `clear(net, layer, geom, width)` with
  `clearance_net_rules`. Property test: on random segments and discs near
  the taz-leaf pads, the index and `run_geometric_drc` disagree 0 times.
  Port the guards, the kink and shortcut checks and via `qualifies` onto
  it, then delete the five route-path pad builders and the `_seg_*`
  helpers.
- D-b. Repair, not withdraw. When a piece fails the oracle (short-risk, no
  via site, unconnected), the realiser tries local alternatives inside the
  branch's corridor triangles, cheapest first: slide the kink along its
  edge (the spoke slot order), kink at the blocker's other side, move the
  via to another index-legal point in the corridor, re-thread that one
  branch around the blocker (`assign_layers`' re-thread move) and kink
  again. Only a branch with no legal alternative fails, and it fails
  alone: the rest of its net and its neighbours stay. This replaces pass
  2, the re-route rounds, the partial filter and the job's failure shove.
  Test: sanity reaches 20/20 with DRC 0.
- D-c. Delete what K and D-b supersede: `_spoke_offsets`' chord,
  boundary-run and Powell special cases fold into one kink routine,
  `_site_via`'s four walks become one index query, `_guarded_branches` and
  `_severed_nets` become assertions, `_merge_pass` and `_copper_zones` go,
  and `RoutePlan` carries one `failed: {branch: reason}`. The
  shape-pinning tests for deleted code are retired in the same commit.
  Invariant tests replace them: committed copper is DRC 0, and every
  complete net joins all its pins including slot pins.

**B3 / E2 / E3, last.** Register a ladder rung that routes the top-K
elites and picks the pose by complete nets (Decision 10). Cache the label
boxes in the anneal (108 s). Target a taz-leaf route of ≤ 20 s (the index
query replaces the 93 s siting walk).

Order: A → K → E1 → C → B1 → D-a → D-b → D-c → B3/E2/E3. A and K are
cheap and change what is measured, E1 baselines every later step, C
removes the largest blocker at compact poses, B1 then gives the router
compact poses, and the D slices change the most code.

## Streamlining

| area | now | after D-c |
|---|---|---|
| `realise.py` | 4951 lines, 62 top-level defs, `_spoke_offsets` 1289 lines, `assign_layers` 403, `_site_via` 267 | ≈1500: `assign_layers`, one kink + repair routine, `sketch_rows`, `widen_*` |
| wire geometry | taut → spoke offsets → 45° L/jog redraw → simplify → coalesce → off-angle refusal | taut → spoke offsets → clear shortcut (K) |
| clearance | 6 pad builders, 6 segment-distance helpers, 7 vetoes | one `CopperIndex`, matching DRC by test |
| recovery | pass 2 + 8 rounds + 3 job shoves, all full re-routes | per-branch rip-up inside one route |
| `RoutePlan` failure fields | `crossings`, `unreachable`, `stranded`, `no_via`, `guarded`, `severed`, `off_angle`, `withdrawn` | `failed: {branch: reason}` |
| tunables | 68 module constants in `topoplace/` | every deleted heuristic takes its knobs with it |
| `pcb_place.py` route half | `_route_failure_pushes`, `_bbox_gap`, `_nearest_pin_part`, the shove loop | ladder rung (B3) |

Kept: `TopoDesign`, the CDT mesh, `build_wires` (global route: homotopy,
capacity, order), pin assignment, the label/silk system, `widen_power`
with the DRC oracle, `wires.json`/masks and the fab overlays.

## Acceptance criteria

1. taz-leaf at seed 1 and two more seeds: ≥ 90 % of multi-pin nets
   complete (slot pins included), committed copper DRC 0, every `near`
   measure within bound, no part outside the outline.
2. Sanity: 20/20, DRC 0 (pcb-topo-place acceptance 9).
3. Status is honest: a net with <2 members is never counted as routed,
   and summary and digest show complete nets and connections.
4. Wires are any-angle with few segments: a realized two-pin net between
   unobstructed pads is one segment, and no check reports `octilinear`.
5. One clearance oracle on the route path. The parity test against
   `run_geometric_drc` passes, and `topoplace/` holds no other
   pad-polygon builder.
6. `realise.py` ≤ 2000 lines. `RoutePlan` has one failure field.
7. The taz-leaf place+route job takes ≤ 4 min.

## Explicitly NOT in scope

A grid or maze router (retired, Reto 2026-11-01); a new placer engine;
blind or buried vias; impedance-controlled widths; capability rows; the
EasyEDA round-trip.

## Open questions / decisions log

- **D1, ruled 2026-11-01:** no grid router. Topo, then kink the wires; the
  realiser repairs a failed kink inside its corridor (D-b).
- **D2, ruled 2026-11-01:** a GND pour on In1 is allowed as an interim if
  it is really needed ("I am not a pour fan, I feel we can do better but
  not just yet"). C tries routed GND on In1 first.
- **D3, ruled 2026-11-01:** a decap or crystal beyond its limit fails DRC
  (A6). The limit is the measure's own goal.
- **Wire angles, ruled 2026-11-01:** free; few segments (K).
- **D4, dropped:** the incumbent rule (B2) waits on E1's airwire ratio.
