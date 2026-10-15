---
status: draft
title: "pcb: a capacity-aware global plan between the annealer and the maze (the loose-routing layer), and where the LLM sits"
prio: high
model: opus
pillar: 3d-design
---

# pcb global plan router

Design session 2026-10-13 (Reto + pcbfixes). Reto: "loosely lay out the
routing at a clever-ratsnest level — move wires around with minimum
waypoints and some vias, without laying copper, so it is fast to rip up;
remove crossovers, minimise vias, pin-swap at that level. When the
algorithm is stuck, describe the situation and let the LLM make the
judgement call, then re-run the algorithm. The EWOD dogfood should be
trivially routable; why can't the algorithm see it?"

## Diagnosis — what the engine runs today, and why dogfood-6 stalls at 51/55

Pipeline (production `pcb_route`): joint annealer `optimize.py::optimize`
(placement + a per-segment layer/side "sketch", cost = USD money terms +
max-of-margins risk) → `realize.py::_realize_maze` (sequential weighted
A* on a hard-occupancy grid, `maze.py::OccupancyGrid`, shortest-first,
12 reorder passes, opt-in PathFinder negotiation on the same fine grid) →
DRC strip → ratchet. Three structural facts:

1. **There is no global routing stage.** The maze is a detailed router
   asked to discover the board-level plan by net order and luck. Its
   search window is the endpoint bbox + 5 mm; its objective is length
   with a 3 mm via cost; early nets commit copper that later nets cannot
   cross. The annealer's sketch (`seg_layer`/`seg_side`, IR level L1/L2)
   is never read by the maze (`realize.py` reads `seg_layer` only in the
   tangent drawer), so the annealer's layer decisions change the cost
   estimate, not the copper.
2. **Negotiation on the fine grid cannot converge here.** PathFinder
   (McMurchie & Ebeling 1995) converges on a small resource graph with
   explicit capacities. On the EWOD grid every cell is a resource, 50 of
   55 nets stay in conflict every iteration, contention is diffuse
   (me477672). That is the expected behaviour of a detailed-grid
   negotiation on a dense fan, not a tuning problem. PCBWorld 2026
   (pa266247) measured the same collapse on OrthoRoute, a PathFinder-style
   PCB router: completion 0.02, 27 vias/net.
3. **Congestion knowledge exists only after routing** (`CongestionWarning`,
   `congestion_cells`) and the only placement-side estimate is a
   nearest-obstacle chord (`gap_capacity`), centroid-granular. Nothing is
   a capacity map the plan can be checked against before copper exists.

Geometry of the stall (fixture `tests/fixtures/pcb/ewod-dogfood-6-replay-v1`):
driver pads 0.8 mm pitch, 2.0×0.5 mm; via 0.45 mm + 2×0.099 clearance =
0.65 mm envelope; a via landed next to a pad leaves 0.15 mm between
neighbours, below the 0.288 mm a track needs. So every inner-layer net
that drops to B.Cu beside its pad kills the B.Cu lane next to it; the
shortest path for every net is the B.Cu path inside the ring, which the
greedy order takes first; the remaining nets need the via row, and the
via row and the lanes compete. Lane count (60 per layer) and grid pitch
were measured not to be the limit. The plan a human draws is: In2.Cu
radially outward from each plaza past the package edge, via in the
4.5–7.5 mm board margin outside the pad row (55 via envelopes on a
~70 mm perimeter), B.Cu stub into the pad's outer end; pin assignment by
angle makes it planar. The engine cannot express "leave the ring and come
back" because nothing in it reasons about capacity: it only sees that
path as 1 via and 6 mm longer.

## Target architecture — three levels and one loop

```
 netlist + intent (classes, measures, annotations)
   │
   ▼
 [A] PLACE   annealer (exists)  + crossing/separation + density terms
   │  poses, rotations, sides, pin-swap freedom
   ▼
 [B] PLAN    global plan router (NEW, ms–s): tile graph with capacities,
   │         negotiated congestion, pin assignment inside the negotiation,
   │         via and layer-direction costs → per-net corridor + layers + via tiles
   │         routability test = every cut within capacity (SURF, DAC'91)
   ▼
 [C] DETAIL  maze (exists) routes each net inside its corridor on its layers
   │
   ▼
 feedback: failed corridor → capacity−1 → re-plan [B]; plan infeasible →
 priced expansion / placement move [A]; still infeasible → LLM menu
```

**[B] is Reto's loose-routing layer.** A net in the plan is a sequence of
tiles with layer changes, i.e. minimum waypoints + vias, no copper.
Ripping one up costs nothing; crossing removal, via minimisation and pin
swaps are operations on this graph. Its routability check is the cut
capacity test of Dai, Kong & Sato (pa266321): a sketch is routable iff
the wires crossing each cut between features fit. He 2024 (pa266350)
reaches the same conclusion from the other side: a global stage that
does not carry via creation and layer assignment produces corridors the
detailed router cannot realise.

Concretely:

- **Tile graph.** Per layer, tiles of ~0.5 mm (one module owns the grid
  math; cost, map and view consume it — the two-sites-drift guardrail
  from `pcb-congestion-driven-spread.md`). Edge capacity between two
  tiles = free span along their boundary ÷ (track + clearance), free
  meaning not within clearance of foreign copper (pads, fixed copper,
  holes). Tile via capacity = via envelopes that fit on every layer the
  via spans. Capacities come from the same stamped copper the maze uses.
- **Negotiated congestion on the tile graph.** PathFinder with present +
  history cost; converges because the graph is small (32 mm board:
  64×64×4 nodes) and capacities are explicit. Edge cost = pitch × layer
  direction penalty (H/V/diagonal as a cost, never a rule — SURF's
  argument, pa266319, and `maze.preferred_directions` already encodes
  it); via edge cost = 3 mm equivalent, same constant as the maze.
- **Pin assignment inside the plan.** For a swap group, sinks are "any
  free pad of the group"; each iteration resolves one-pad-per-net by
  Hungarian on negotiated path cost (`pinswap._hungarian` exists). This
  replaces the separate warm start (+9 nets on EWOD) with the same lever
  applied where it sees contention. Part rotation stays a placement move.
- **Corridor-guided detail.** The maze routes each net with its search
  window masked to the corridor (dilated by one tile) and `layers=` from
  the plan, most-constrained net first. The 12-pass reorder remains the
  fallback for leftovers. This also makes the maze faster, not slower.
- **Loop.** Detailed failure in a corridor lowers that edge's capacity
  and re-plans (cheap). A plan that cannot converge yields a **min-cut
  witness**: the saturated edges with the nets and features on each side.
  That witness is what the expansion moves (`OUTLINE_GROW`, `LAYER_ADD`,
  already built in `anneal_route_loop`) and the placement anneal act on,
  and what the LLM reads if those are capped.

**[A] gains two cheap terms**, both computable from the tile grid: a
density prior (courtyard area per tile above a threshold) and a net
separation/crossing term (NS-Place, pa266391: a smooth crossing-number
bound; measured up to 50 % fewer vias and 79 % fewer DRVs with
Freerouting as the evaluator). `gap_capacity` is retired when the tile
capacities exist.

## Where the LLM sits (and where it never does)

Evidence first. OmniLayout 2026 (pa266419): LLM zero-shot placement has
overlap rate 0.26 and routability ratio 0.34; with tools and feedback,
overlap 0.13, routability 0.91, but 9 vias/board vs 1 and "significantly
below human engineers". PCBWorld 2026 (pa266247): GPT-5.4 interactive
routing completes 0.65 vs Freerouting 0.80, 30× slower, and open-loop
LLM routing fails on parse errors; the LLM works only when every move is
executed and checked by the engine. False Dawn 2023 (pa266394): SA and
commercial tools beat RL macro placement. He 2024: an RL/MCTS router
solved 2 of 1182 boards and lost to the author's own heuristic.

So the boundary is:

1. **Intent → constraints, before the solver.** The LLM turns circuit
   knowledge into the vocabulary the engine already has: net classes,
   measures (`proximity`/`separation`/`align` with `soft`/`hard`/
   `invariant`), `function_hint`, plane nets, keepouts (once they bind —
   `pcb-keepout-does-not-bind.md`). New vocabulary worth adding here, as
   measures not as prose: `noise_source` / `noise_sensitive` nets with a
   separation distance, and keep-close for decoupling (today a proximity
   measure). This is where "heat maps" of intent come from: rf-noisy and
   dense regions are measures the tile grid can rasterise.
2. **Stuck → a priced, discrete menu.** The deterministic loop ends in a
   `ConflictDigest` (exists): binding constraints, witnesses, marginal
   USD. The LLM chooses among moves the engine then executes and
   legalises: unlock a fixed part or rotation, grow the outline, add a
   layer, relax a class rule, allow via-under-body, change a measure's
   strength, pick a fanout style (inside-ring / outside-ring / staggered).
   Never coordinates, never a trace, never net order (Reto 2026-10-12:
   "do not patch routing order"). The menu's items must each be a
   one-token change to the IR followed by a re-run, so the LLM's error
   mode is "a worse priced result that the ratchet refuses", never an
   invalid board.
3. **Result → review against intent.** After a clean route the LLM reads
   the board as text handles (`argue.py` grammar) plus measures, and may
   file a measure it believes is violated in spirit; the engine re-runs.
   Images are secondary evidence (OmniLayout: visual feedback helped
   routing, numeric feedback drove convergence).

What the LLM does NOT do: run the inner loop, choose which net to rip,
propose rotation angles numerically, or "unlock" anything without a
witness that names the saturated cut.

Shipped systems draw the same line (Perplexity pass 2026-10-13, web
sources below, vendor claims marked): PCBAgent (ASP-DAC 2025) has the
LLM translate user text into commands of a placement-adjustment DSL that
an RL placer executes, reaching 90 % of manual HPWL on 17 industrial
boards; the Chalmers ECCE Europe 2025 router has the LLM read a JSON net
description and emit A* cost weights and priorities, the A* routes;
Trace (vendor doc) has the LLM analyse constraints and pick the winner of
several SA runs; Flux Copilot admits "limited understanding of layout and
trace positioning" and only reaches its router through net rules.
PCB-Bench (ICLR 2026) finds "substantial gaps" in spatial placement
reasoning. No published system has an LLM read congestion maps, rank
rip-ups or choose pin swaps; those are listed as future work. Our menu
design is the proven pattern (LLM → one DSL-level change → engine), not
the unproven one.

## Build order (Devin-sized slices; each lands alone)

0. **Probe — DONE 2026-10-13** (`pcb-global-plan-probe.md`, code
   `globalplan.py`, test `test_pcb_globalplan_ewod.py`). On a 0.5 mm tile
   model all three pin assignments converge to a capacity-feasible 55/55
   plan in 7–9 PathFinder iterations and under 3 s; the corridor hand-off
   to the maze as built (contested-cell stamping) routed 38–42/55, below
   the 51/55 baseline, because stamped walls forbid the via row the inner
   hops need — so it was thought: `OccupancyGrid.route(mask=)` (built the
   same day, via disks free to overhang) gives 38/41, the same loss. The
   corridors are wrong at the detailed level because the tile capacities
   overestimate (participant keep-outs read free, through-vias uncharged).
   Corridors as a cost (slice 0b(b), same day): 47 (fewest-via plan) /
   45 (distance plan) vs 51 plain; a 4x expansion budget routes fewer
   (45), so the residual is not budget-bound. Strict capacities (slice
   0b(a): participant copper blocks lanes, through-vias charged) wall off
   17–20 nets — 35–38/55 planned, final 40/47. The 0.5 mm boundary-run
   capacity brackets the fine grid (open 55 ≥ 51 ≥ strict 38) without
   locating it. Reading: the plan level is cheap, but on this board no
   plan-guided variant beats the plain router; slice 1 needs capacities
   from the fine grid's own connectivity (per-tile max-flow over free
   cells, or smaller tiles), not a setting of the current measure.
   Benchmarks: the replay (51) and the plaza fanout member (53/55).
1. `globalplan.py`: tile graph + capacities from stamped copper; a text
   capacity map in `view='congestion'` ("no map yet" ≠ cool).
2. Negotiated plan with pin assignment folded in; `RoutePlan` persisted
   per route (corridors, layers, via tiles) next to the sketch; the
   min-cut witness in the digest.
3. Corridor-guided maze routing in `pcb_route`: corridors as a cost
   first (slice 0b measures hard `mask=` vs priced off-corridor cells),
   default on only when it beats the plain 12-pass on both benchmarks;
   ratchet unchanged.
4. Loop: corridor failure → capacity−1 → re-plan; infeasible plan →
   `anneal_route_loop` expansion moves (surface wiring is the open item 1
   of `pcb-anneal-closes-router-loop.md`; brief
   `/tmp/pcb-devin/devin-pcb-loop-surface.txt`).
5. Placement terms: density prior + net separation on the tile grid;
   retire `gap_capacity`.
5b. Seed legalisation — DONE 2026-10-14 (gr481705): the judged `put`
   now runs `OptimizeEngine.legalize_start` over freshly-seeded
   generator components before the always-valid gate reads the board,
   and UNPLACES (x/y := NULL) a movable secondary whose seed still
   conflicts with the generator's own authored vias — the anneal's
   legal islands can be sub-lattice thin (ewod-dogfood-6's sink island
   is ~0.01mm wide; the 0.25mm ring search cannot land on it), and an
   unplaced part is the one incomplete-but-valid state the gate permits
   while `op='place'` owns the pose. A `fixed` seed keeps the refusal
   verdict. The ordinary re-apply already carries the stored pose
   forward (abf7e0ece). Residual for the global router proper: seeds
   legalised-or-unplaced still rely on the anneal to rediscover the
   island every regenerate — a capacity-aware seed (slice 0's planner
   feeding placement) would emit a corridor-aware pose directly.
6. LLM menu op: digest → menu → one IR change → re-run; the noise
   source/sensitive measures.

Items this supersedes or corrects: `pcb-layer-preferred-direction.md` is
stale (the maze has H/V/diagonal preferences, `maze.preferred_directions`,
off-axis ×1.6); keep only its "anneal the angle" idea as a plan-level
cost. `pcb-congestion-driven-spread.md` parts 1–2 become slices 1 and 5
here; part 3 is slice 4. Fine-grid negotiation (`negotiate=`) stays
opt-in and is not a default candidate.

## Benchmarks (for the paper, not for this build)

One board proves nothing: He 2024's 10-board ranking reversed on 1182
boards (Freerouting 72.5 %, PcbRouter 43.5 %, pad-focused 48.1 %).
Our set: dogfood-6 (51/55), Nano USB-PD (38/64 → 43/64 with expansion),
esp32c3 reference 5 seeds, heater base (55/89), and Reto's TAZ Control
Board V1.15 (imported 2026-10-13 as prod `taz15` with the source copper
fixed = the human yardstick, and `taz15-blank` with `--copper none` = the
routing target: 96 parts, 151 nets, 386 connections, 4 layers with two
inner planes, 79 nets carry source copper). Synthetic family
`tests/fixtures/pcb/qfp_fanout.py` (80 pads at 0.8 mm, 55 nets, sources
on a 3 mm grid, In2+B.Cu, seed 1; 2026-10-13): cold 46/55 with 48 vias
in 20.7 s, distance warm start 55/55 with 4 vias in 0.9 s — the bare
mechanism is trivially routable. The same 55 sources packed into nine
plazas at the 0.65 mm via envelope (`sources="plaza"`): cold 41/55 with
64 vias, warm 53/55 with 35 vias in 4.7 s — the plaza packing alone
reproduces the dogfood's wall (51/55), so the family is the benchmark
for slices 1–3 and the plaza member is the one to beat. Candidate external sets:
He 2024's 1182-board JSON benchmark; PCBWorld's 679 real KiCad boards
and 8 engine-checked metrics (pa266247); DeepPCB's published yield
(median 100 %, mean 95.6 % completion over 32k jobs; 4-layer 51–500
airwires: 72–90 % of boards ≥95 %; ~0.28 vias/airwire — vendor numbers).
`pcb-paper-benchmark-selection.md` holds the selection question.

## References (precis paper ids; `get(kind='paper', id=..., view='abstract')`)

- pa266319 Dai, Dayan, Staepelaere, "Topological routing in SURF:
  generating a rubber-band sketch", DAC 1991 — crossing-point assignment
  per cut with layer, CDT region graph, no preferred direction.
- pa266321 Dai, Kong, Sato, "Routability of a rubber-band sketch", DAC
  1991 — per-layer cut-capacity routability test.
- pa266334 Speckmann & Verbeek, homotopic rectilinear routing with thick
  edges, GD 2009 — from accepted topology to thick traces with few bends.
- pa266350 He, PhD thesis Iowa State 2024 — polygon-partition global
  stage with via/layer decisions, MCTS, A* detail, tree-search rip-up;
  1182-board benchmark; Freerouting/PcbRouter/DeepPCB numbers.
- pa266391 Cheng, Ho, Holtz, "NS-Place", ASP-DAC 2022 — net-separation
  gradient placement; up to 50 % fewer vias, 79 % fewer DRVs.
- pa266247 PCBWorld 2026 — KiCad-grounded routing environment; RL/LLM
  agents vs Freerouting/OrthoRoute/KRT; LLM failure modes.
- pa266419 OmniLayout 2026 — 1681 schematic-coupled boards; LLM
  placement benchmark with tool feedback; overlap/routability numbers.
- pa266394 Cheng, Kahng et al., "The False Dawn", 2023 — RL macro
  placement loses to SA and commercial tools.
- pa701 OpenROAD, DAC 2019 — 2D global route then ILP layer assignment.
- pa201822 via-programmed structured ASIC — PathFinder present 4000 /
  history 0.5 converging in ≤8 iterations, then re-place with whitespace.
- pa266386 LLMs for EDA survey 2025 — GPCB summary (unverified claims).
- Not in the library, cite from bibliographies: McMurchie & Ebeling,
  "PathFinder", FPGA 1995, doi 10.1145/201310.201328; Yan & Wong 2010
  escape routing; Ma, Young, Wong DAC 2011 layer assignment of bus escape
  routing; Luo et al. ISPD 2010 B-escape.

Web sources (Perplexity sonar-deep-research, 2026-10-13; cached as
`perplexity-research` 481905 and 481908; unverified, import before citing):

- Yan et al., ASP-DAC 2021, doi 10.1145/3394885.3431568 — grid A* with
  per-cell congestion + DRC cost, negotiation across iterations, global
  corridor stage; 100 % routability, zero DRC on 14 benchmarks where
  DeepPCB and Freerouting fail several. Closest published design to [B]+[C].
- kicad-tools `GlobalRouter`/`NegotiatedRouter` (deepwiki.com, repo
  rjwalters kicad-tools, page "4.3 routing algorithms") — tile
  graph with geometric edge capacities, negotiated corridors, adaptive
  history increments, A-B-A-B oscillation detection, per-net time caps.
- KiCadRoutingTools fanout (deepwiki.com, repo drandyhaas
  KiCadRoutingTools, page "11 fanout systems")
  — QFP: pad → side → short perpendicular stub, stubs become obstacles;
  BGA: escape-channel detection, pad-to-channel assignment, rerouter.
- PathFinder, FPGA 1995: cecs.uci.edu/~papers/compendium94-03/papers/1995/fpga95/pdffiles/6a.pdf
- DeepPCB.ai benchmark 2026 (deeppcb.ai/pcb-autorouter-benchmark-2026/) — vendor numbers above.
- Altium Situs (altium.com documentation, "Situs topological autorouter")
  — topological map, first pass ignores conflicts then raises collision
  cost; per-layer direction and "Via Miser" strategies. TopoR/Eremex —
  rubber-band nets, lead-equivalence swaps, any-angle; marketing only.
- PCBAgent, ASP-DAC 2025 (cse.cuhk.edu.hk/~byu/papers/C247-ASPDAC2025-PCBAgent.pdf).
- Chalmers, "Leveraging LLMs for Intelligent Power Electronics PCB
  Routing Optimization", ECCE Europe 2025, doi 10.1109/ECCE-Europe62795.2025.11238369.
- DATE 2024, "Learning Circuit Placement Techniques through RL with
  Adaptive Rewards" — 17–21 % lower post-routing wirelength vs SA.
- PCB-Bench, ICLR 2026 (iclr.cc/virtual/2026/poster/10009621).
- Trace (docs.buildwithtrace.com, concepts, "pcb layout") — SA with RUDY
  congestion in the cost; LLM constraint analysis; vendor doc.
