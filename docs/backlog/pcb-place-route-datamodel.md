---
status: parked
title: "pcb: smooth place-and-route — topology by search, layers by belief, geometry by batched relaxation, coarse-to-fine"
prio: high
model: opus
pillar: 3d-design
---

# pcb place-and-route: state, energy and method

Source: Reto with witty (gripe-fleet coordinator), 2026-10-20 ~13:00–15:00Z,
during the place/route HALT (memory me500154). A first draft was reviewed by
Fable (~12:45Z); this version folds in that review and Reto's rulings since.
Agreed by Reto 2026-10-20. It replaces the method of [pcb-unified-place-route](pcb-unified-place-route.md)
and builds on [pcb-vector-router](pcb-vector-router.md) where §Conflicts says;
where they disagree, this file wins.

**Superseded (Reto 2026-10-25).** `precis.pcb.pr` is deleted: its
slice 2 results beat vroute on no bench.
[pcb-topo-place](pcb-topo-place.md) is the router path forward; this
file stays as the record of the smooth-energy design.

## Reto's position

Paraphrased from the session; his words where quoted.

- Tracks, pads and parts are plain vector rows that batch on a GPU; parts
  rotate and flip as array operations.
- Relax the whole circuit: rigid, movable, rotatable parts with flexible
  wires; minimise length and vias; grow the wires to width.
- Discrete choices belong inside annealing.
- "The datastructures drive in many ways the method"; memory is not scarce
  at 10–100 components, so question every structure that exists to save it.
- Layers are native: alternatives on every layer exist in parallel and via
  position is a continuum; the model must see them together. A human moves
  things between layers to find a compromise; the optimiser should do that
  for all nets at once, probabilistically.
- "I don't like these grids." Smooth and continuous.
- Not a proven optimum: "a local minimum that is awesome".
- Track angles: "entirely arbitrary and I don't care". Part rotation 90° or
  45° preferred, "but really we should not care"; snapping and gridding are
  visual sugar in the final pass.
- GND is no special case: build a ground tree like any net. No separate
  pour: "can we just widen the wires above their minima? That is a smooth
  continuation of what we already do." GND and power want to be large;
  sensitive nets want little copper. Capacitance goes general for now: wire
  area ∝ capacitance.
- Blocks get movable pins (any layer, any location) carrying the combined
  properties of what is inside (50 mA + 100 mA, …).
- Nets carry v_min, v_max and f_max; currents come from circuit maths where
  possible (high-impedance inputs, pull resistors at v_max).
- Through vias only for the foreseeable future.
- PyTorch for now; tolerant determinism.
- The language model supplies intent and pin facts at netlist time; the
  constraints it writes should make mid-run review unnecessary; it does read
  failure reasons. Datasheet layout review already happens at design time.
- The layer loop "probably settles one way or the other with time"; the
  swept collision check is required.
- Spec agreed 2026-10-20 ~14:30Z. The grid router is removed from the tree
  now, in one deletion commit, "even if it is a short term regression"; the
  halted Devins' work is used or deleted as appropriate.

## Preconditions

A board enters place-and-route only when all of these hold; a pre-route check
reports each missing item as an actionable error.

1. A closed board outline (otherwise DRC fails and nothing routes; g-S's
   `outline_missing` rule).
2. Every part's footprint, pads and courtyard.
3. Every pin with a current and every supply pin with a direction (source
   or load); every supply net paired with its return net; every net with the
   attributes in §Net attributes. The language model extracts datasheet
   values when it generates the netlist; currents through passives come from
   a DC estimate (pull resistor I = v_max / R, LED (v_max − V_f) / R,
   high-impedance input ≈ leakage). Each current records its source
   (datasheet, computed, estimate) and estimated currents size
   conservatively. Extraction quality is the datasheet skill's concern, not
   the router's; there is no confirmation step.
4. The stackup: layer count (4 now, up to 12), heights, dielectric, copper
   weight. All layers are routable; there are no reserved plane layers.
5. Groups and constraints from the netlist step (§Hierarchy).

## State

Plain arrays indexed by integer, capacity-padded with a live mask so shapes
stay fixed for compiled kernels; freed rows go on a free list. Built from
PcbIR (`ir.py`), which keeps its role as the input.

### Parts

| Column | Meaning |
|---|---|
| `pos[n,2]` | centre (mm) |
| `rot[n,2]` | (cos θ, sin θ), free during the solve |
| `side[n]` | top or bottom; changed only by a discrete move |
| `lock_xy[n]`, `lock_rot[n]` | the IR's two lock bits (`inst_fixed_xy`, `inst_fixed_rot`) |
| `cluster[n]` | parent per hierarchy level |

### Pads, holes and other static shapes

Each is a core plus a radius (§Shapes) with a layer set.

| Thing | Layer set |
|---|---|
| SMD pad | its side's outer layer (mirrored by `side`) |
| Through-hole pin, plated hole | every layer |
| Non-plated hole | every layer (drill-to-copper clearance; no copper) |
| Via | every layer (through vias only) |
| Keep-out | the layers it names |
| Authored fixed copper | its layer; an obstacle and, for its net, a terminal |
| Board outline | every layer (edge clearance) |

### Nets as trees

Every net, GND included, is a tree: pins are leaves, Steiner points are free
junctions. The tree's shape is a variable; a discrete move re-plans it. A
net with negative appetite may add extra edges (a mesh, not a tree) where
an edge buys more copper area than it costs; that is how widened ground
closes the gaps between its strips.

Current on each tree edge follows from the pin currents: solve `B·f = i` with
B the tree's incidence matrix and i positive at sources, negative at loads.
In a tree the solution is unique. Each edge's minimum width follows from its
current (IPC-2152 for the stackup's copper weight), so width changes at
Steiner points. A net that ends up with loops (parallel paths, or a pour
joined at several points) divides current by conductance: one Laplacian solve
`G·φ = i`, conductance ∝ width / length, gives voltage drop and per-edge
current.

### Net attributes

| Attribute | Drives |
|---|---|
| `v_min`, `v_max` | spacing (`rules.required` voltage tier), resistor currents |
| `f_max` (or edge rise time; f ≈ 0.35 / t_r) | return-path weight, area cost for fast nets |
| `role` (power, ground, signal, sensitive) | default copper appetite |
| `appetite` α | cost per mm² of the net's copper (below) |

**Copper appetite.** Capacitance is modelled generally as proportional to a
net's copper area, so one term covers pours, decoupling and sensitive nodes:
`E_area = Σ_nets α_net · area_net`. Power and ground have strongly negative α
(they want area: their wires widen above their current minimum until other
nets' spacing stops them, and the plane emerges); ordinary signals α ≈ 0
(minimum width); sensitive and fast nets positive α (as little copper as
possible). α is set from `role` and `f_max` and can be overridden per net.
The appetite weight must stay below what a signal's detour costs, or ground
shoves signals aside; that balance is tuned on the bench. Width above a
net's current minimum is slack-filling, not demand: the capacity check
(4c) counts every net at its minimum, and appetite widening only takes room
left over. Pads of a negative-appetite net join its copper solidly (SMD)
or with thermal-relief spokes (through-hole). A finer
capacitance model (between specific net pairs, by layer distance) can
replace this term later.

### Wires, spans and vias

- A **wire** is one tree edge: a chain of **spans** joined by **vias**.
- A **span** is a polyline on exactly one layer: a chain of points joined by
  straight pieces.
- A **via** is a bead on the wire at a layer change. Through vias only: the
  bead is a point that exists on every layer at once, with the via pad's
  radius, so it must be clear of other nets' copper on all layers at the same
  time, and moving it moves it on all layers. Each layer's copper pushes on
  it.
- A heavy net changing layer needs ⌈I / I_via⌉ vias in parallel; they are
  one bead cluster that moves together.
- Same-net via spacing (hole-wall rules) and no-via-in-pad hold between
  beads and pads (`rules.required`, `rules.py:556`).

### Layer beliefs

`Q[spans, L]`: for each span, the probability of each layer while layer
assignment is being decided (§Method 4b). Geometry is always evaluated on
hard assignments drawn from Q, never on Q itself.

## Shapes and distance

A shape is a skeleton (core) plus a thickness (radius r):

- track piece: a segment, thickened by width / 2 (a strip with round ends);
- round pad, via, through-hole pin: a point, thickened by its radius (a disc);
- rectangular pad: a box with r = 0, or a small r for rounded corners;
- static polygons: outline, keep-outs, fixed copper.

    gap(a, b) = dist(core_a, core_b) − r_a − r_b − required(a, b, layer)

`required` is the spacing rule for that pair (`rules.required`: net class,
kind, voltage, coating, per-layer tier). `gap ≥ 0` is legal. Two shapes
interact only where their layer sets overlap; holes, plated holes,
through-hole pins and vias overlap every layer.

Core distances are closed forms per pair of kinds, batched and
differentiable away from kinks: point–point, point–segment, segment–segment
(the kernel's forms, `clearance.py:116-201`), plus box and polygon edges.
Segment–segment, not point–point: points about 1 mm apart against a 0.15 mm
spacing would let wires slip between samples. Growing a wire raises its r;
the cores do not change. The exact kernel (`clearance.py`, shapely core
distance) stays the final judge.

## Hierarchy

Levels form a tree: board → blocks → sub-clusters → parts. Usually 2–3
levels at 10–100 parts.

- **Blocks** come from the language model at netlist time, with meaning: a
  buck converter, the MCU with its caps and crystal, USB with its ESD parts.
  They are the schematic sheets. With them it writes constraints (keep
  together, crystal within 5 mm of these pins, antenna area clear, connector
  on the edge), recorded before the run so replays need no model call.
- **Sub-clusters** come from connectivity clustering inside and across
  blocks.
- **Block ports.** Where a net crosses a cluster's boundary, the coarse
  level has a port: a movable point (position a variable, on or near the
  boundary; layer a belief like a span's). Its properties combine what is
  inside: current = the sum of that net's pin currents inside the block
  (the tree edge crossing the boundary, from `B·f = i`), v_max and f_max =
  the maximum over members, role = the most demanding. A **cable** bundles
  the ports between one pair of clusters; its width is the sum of member
  widths plus spacing.
- **What a cluster looks like at the coarse level**, per layer: the
  members' real pads and holes (already rows in the table) without the
  internal wires, plus a soft reserve for internal wiring (estimated
  internal copper area spread over the cluster's area). On the cluster's
  own outer layer that is pads plus reserve; on inner layers only
  through-hole pins and expected internal vias; on the far side, open
  unless parts sit there. Routing through a cluster is priced, not blocked.
- Each level has **restrict** (parts → cluster, nets → ports and cables)
  and **prolong** (cluster pose → member poses at their offsets; each port
  becomes the point where its member wire crosses the boundary, which fixes
  the fan-out order; cable → member wires). A cable keeps its members in
  order; a wrong order would force twists, so the order is state
  (unified D3).
- **Wire refinement** is a second axis: each wire starts with about 4 points
  (roughly which way it goes and which side of things it passes); after it
  settles, a point is inserted between each pair (8, 16, …) until the spacing
  fits the shape (about 0.5 mm, closer on bends).

Solving coarse first keeps the step count from growing with board size: a
P-point wire straightened by plain descent needs about P² steps, a part's
move must travel through its wires, and warm-started refinement needs a
constant number per level. The barrier's stiffness still bounds the step
size at every level (§Risks).

v1: the language model's blocks plus wire refinement. Algorithmic
sub-clustering is kept if it beats v1 on the bench.

## Goal

"Awesome" (Reto): it works, every net is routed reasonably, no DRC or ERC
errors remain, and it finishes in reasonable time. The energy and its
weights are the means; the bench numbers (routed, DRC, ERC, wall time,
against the frozen grid table at main 29d131207) are the measure. Weights
are tuned to the bench.

## Where parts move

| Where | Move | Mechanism |
|---|---|---|
| 4-0 starting placement | large | blocks placed with no wires |
| Coarse levels | large, cheap | whole blocks move with their ports and cables while wires have few points |
| 4c relaxation | small, continuous | poses are variables beside the wires: wires pull and push, parts slide and turn |
| 4d discrete moves | jumps | relocate a part, rotate it 90° or 180°, flip side, swap parts, swap pins; each rips the part's wires and re-inserts them (4a) |

Once fine-level wires exist, the swept collision check stops a part's pads
from passing through a same-layer wire, so relaxation only polishes within
the gaps the wires leave. Large placement changes at fine levels are
therefore always discrete moves with re-insertion, aimed at parts with long
or stuck wires; the bulk of the shuffling happens at the coarse levels.

## Loop nesting

    for level in hierarchy (coarse → fine):          # prolong between levels
        for copy in population (T_1 … T_C):          # replica exchange between copies
            repeat until no copy improves:            # outer round
                4b  layer beliefs (this copy's Q)
                4a  re-insert spans that changed layer
                4c  relax K samples, growth schedule inside
                4d  update Q (CEM), propose discrete moves
        each batch on the GPU is C × K boards

Stopping: a level ends when no copy improves for a set number of rounds,
or at the job's wall-time limit; the best board and its per-connection
reasons are reported.

## Method

Run at each hierarchy level, coarse first.

### 4-0. Starting placement

The topmost level starts without wires: blocks are placed by relaxing
straight-line pulls between connected ports plus overlap repulsion and
hold-in (the existing placer, `optimize.py` / `field.py`, does this). Each
finer level starts from the prolonged coarser answer.

Layers at this stage: there are no wires yet, so layers enter only through
the parts themselves. A block's side (top or bottom) is a choice here, parts
on opposite sides do not collide, and through-hole parts block both sides.
Routing layers start to matter at 4a.

### 4a. Topology seed (search)

Insert each connection into per-layer triangulations (d-cdt's CDT, funnel and
portal capacities) as a crossing-free path, pricing each portal at the final
width. When a path is blocked, rip up the connections the cheapest path names
as blockers, insert, reinsert them, depth-first under a per-connection
budget. The result fixes which side of each obstacle each wire passes and the
order of wires across each triangulation edge. Planarity comes from this
search and is kept by the collision check in 4c; the energy does not create
it.

The first seed puts each connection on its cheapest layer greedily. When 4b
moves a span to another layer, that span is re-inserted on its new layer
before 4c, so 4b → 4a (changed spans) → 4c is one inner loop. The
triangulations are rebuilt from the current geometry every outer round, with
the wires as constraints, and the topology is read back off them; parts
moving in 4c need no incremental bookkeeping.

### 4b. Layer beliefs (all nets at once)

Spans are bounded by candidate via sites: along each wire's current path
every few millimetres, plus each Steiner point, kept clear of pads.

Costs, rebuilt from the current geometry each round:

- per span and layer `U[s,l]`: length, congestion on that layer, distance
  from the supply's return tree on that layer (loop area);
- between spans `P[s,t,l,m]`: when both are on one layer and their paths
  cross or crowd, the cheaper of the detour length on that layer and the
  cost of two vias; zero across layers;
- along a wire: via cost on a layer change, × the parallel-via count.

Two batched updates, alternated, with temperature T annealed:

1. along each wire, exact forward–backward over its chain of spans: every
   placement of 0, 1, 2, 3… vias weighed at once (spans × L² per wire, all
   wires in parallel);
2. across wires, mean field: `Q ← softmax(−(U + P·Q) / T)`.

Then draw K hard assignments from Q (K ≈ 16): K complete boards, each with
every span on one definite layer.

### 4c. Geometry relaxation (batched)

Relax the K boards at the same time, as one batched computation on the GPU
(about the cost of one), and score each. Variables: part poses, Steiner
points, via beads, block ports (coarse levels), span points, widths, label
poses.

| Term | Effect |
|---|---|
| Length | tension along real routes; pulls parts together |
| Via | per via bead cluster |
| Segment barrier | same layer, different net: wires push each other and parts |
| Via and hole barrier | beads and holes against every layer |
| Courtyard overlap and reserve | parts keep spacing (the engine's reserve, `field.py`) |
| Hold-in, keep-outs, fixed copper | stay inside and clear |
| Loop area | supply spans run over their return tree, on near layers |
| Voltage drop | rewards width on supply nets |
| Copper appetite | α_net × area: ground and power widen, sensitive nets shrink |
| Width floor | from edge current |
| Bend radius | centreline radius ≥ track width |
| Labels | silk labels near their part, clear of pads and each other (unified D4) |

A swept collision check rejects or shortens any step that would make two
same-layer wires cross, so the 4a topology holds. Widths grow from zero to
full over the relaxation. Before each growth step every triangulation edge's
capacity is checked at the target width; the first edge over capacity stops
growth there and becomes either a 4d move or the failure reason, instead of
the barrier stalling. Pin pulls and part torques are the gradient of this
energy through the pose transform, by autodiff.

### 4d. Learn and move

Each relaxed sample's measured energy updates Q. v1 uses the cross-entropy
method: move Q towards the layer assignments of the best few samples, which
needs no per-choice credit model. Thompson sampling over a learned per-span
cost correction (the cost model as prior, relaxed boards as observations)
replaces it only if CEM stalls.

Compute is K samples × copies × levels × rounds × relaxation steps. The job
takes a wall-time limit (`deadline_s`); the default shape is about 4 copies
× 16 samples, and the prototype measures what fits. Discrete moves are proposed across a
population of copies at different temperatures, accepted by Metropolis, with
neighbouring temperatures swapping states:

- relocate a part (or block) to another spot, re-inserting its wires;
- rotate a part by 90° or 180°, re-inserting its wires;
- flip a part to the other side (`MoveKind.SIDE_FLIP`);
- swap same-footprint parts (`MoveKind.SWAP`), swap interchangeable pins
  (`pin_swap_groups`);
- re-plan a net's tree;
- re-insert a stuck wire's topology (4a).

Repeat 4b–4d until no improvement, then prolong to the next level.

## Finish

1. Rotation sugar: per part, try 90°, then 45°; keep it only if the board
   stays legal and no worse. Same rule for light gridding of part positions.
2. Clean up wires: drop points that are not needed and fit the rest to few
   segments and arcs within a tolerance that keeps spacing. No angle snap.
3. Turn each net's copper per layer into a polygon (the union of its
   widened wires, outline smoothed), so widened ground reads as a pour.
   Fill remaining slivers that touch exactly one negative-appetite net with
   that net, within spacing; remove islands; check necks. This is geometric
   clean-up, not a second routing model; there is no separate pour stage.
4. Exact DRC with the kernel.
5. Write `pcb_copper` with the connection per row, and save the route state
   so a later run resumes from it.

## Results and failures

- Success: the board and its numbers (routed, DRC errors, vias, length,
  wall time).
- Failure, per connection, an actionable reason: what blocks it, where, by
  how much, and the knob that would help (`UnroutedReason`, `realize.py`).
  Example: "the triangle edge between U1.3 and C4 needs 0.4 mm more on every
  layer; move C4 or narrow the class". The language model reads these and
  proposes a fix or asks Reto.
- Determinism: same board and seed give the same result within a stated
  tolerance; tests assert the tolerance.

## Size and hardware

| Term | Cost per evaluation |
|---|---|
| Segment barrier | O(N²) dense; O(N) with neighbour lists |
| Part pairs | O(n²), negligible |
| Length, via, width | O(N) |
| Layer beliefs | O(spans × L²) along wires plus the sparse P·Q product |

N is the number of segments. PyTorch with a pairwise kernel that never builds
the N×N matrix (KeOps or Triton), on the DGX Spark's GPU, and a CPU path for
tests. Naive dense autodiff on nano (~8,000 segments) is memory-bound at
about 10–20 ms per evaluation; the kernel route is the target. Numbers are
measured on the prototype, not assumed.

## Dependencies outside the router

- The viewer must show schematic sheets (the blocks).
- The EasyEDA exporter must carry sheets.
- The netlist step must emit pin directions, currents, return pairs, blocks
  and constraints.

## Conflicts with existing specs

- pcb-unified-place-route.md: taut bands and the piecewise-smooth energy
  (slices D–F) are replaced by 4b–4d; D3 (band order), D4 (labels), D5
  (rotation order), D13–D14 (PDN loop, supply trees) and D16 (via
  keep-outs) carry over in this form; D15 (pours as cost, grow_to_fill) is
  replaced by copper appetite; Decision 10 was already replaced.
- Any existing plane-layer handling (`net_plane_layers`, plane fan-out,
  drop vias, `_pour_planes`) is replaced by appetite-widened nets; no layer
  is reserved for a plane.
- pcb-vector-router.md: Decision 2 and `drc.check_octilinear` (45° house
  rule) are dropped per Reto; Decision 10 / unified D9 (no GPU, CPU path for
  tests) becomes "GPU allowed, CPU path for tests"; Decision 17 (through
  vias) stands.
- The halted Devin WIP is assessed after this spec is agreed.

## Risks

1. The schedule (temperature, width growth, barrier stiffness) decides
   quality; it is measured on the bench.
2. The barrier is stiff; step-size control is required.
3. The cost model behind Q may mispredict; the sample-and-measure loop is
   the correction and costs K relaxations per round.
4. The swept collision check can freeze progress in tight spots; the
   discrete moves are the exit.
5. GPU determinism is tolerance-only.
6. Copper appetite against signal routing: too strong and ground shoves
   signals aside, too weak and the plane has holes; a bench-tuned weight.
7. Estimated pin currents may undersize copper; estimates size
   conservatively and are listed for confirmation.

## Frozen reference — "grid at 29d131207"

Reto 2026-10-20 cutover ruling: the grid router is retired NOW and the
default flips to `router='vector'`; the maze is deleted outright (it
lives in git). This table — the maze at 29d131207, measured
2026-10-20 on the extended set (nano seeds 1–8, nano-r2, esp32c3 seeds
1–5, ewod-replay, sanity-straight, qfp-plaza; dogfood-b has no pinned
fixture, taz15-blank snapshot absent) — is the frozen reference every
vector number is compared to, "match" defined against it.

| board | routed/nets | failed | conns | vias | wire mm | wall s | s/net | s/conn | DRC s | DRC errors | DRC warns |
|---|---|---|---|---|---|---|---|---|---|---|---|
| nano | 41/64 | 23 | 206 | 141 | 2867.8 | 84.3 | 1.32 | 0.409 | 47.19 | 65: connectivity 9, silk_missing 33, unrouted 23 | 11: board_edge_clearance 2, clearance 9 |
| nano-s2 | 39/64 | 25 | 206 | 106 | 2671.5 | 86.6 | 1.35 | 0.420 | 19.07 | 69: connectivity 5, silk_missing 39, unrouted 25 | 12: board_edge_clearance 3, clearance 9 |
| nano-s3 | 46/64 | 18 | 206 | 126 | 2754.3 | 85.0 | 1.33 | 0.413 | 27.84 | 61: connectivity 9, silk_missing 34, unrouted 18 | 11: board_edge_clearance 2, clearance 9 |
| nano-s4 | 40/64 | 24 | 206 | 134 | 2652.1 | 83.8 | 1.31 | 0.407 | 31.37 | 73: connectivity 10, silk_missing 39, unrouted 24 | 11: board_edge_clearance 2, clearance 9 |
| nano-s5 | 40/64 | 24 | 206 | 141 | 2778.0 | 86.8 | 1.36 | 0.421 | 39.34 | 75: clearance 1, connectivity 11, silk_missing 39, unrouted 24 | 18: board_edge_clearance 2, clearance 9, silk_edge_clearance 7 |
| nano-s6 | 37/64 | 27 | 206 | 121 | 2460.2 | 102.2 | 1.60 | 0.496 | 28.88 | 75: connectivity 9, silk_missing 39, unrouted 27 | 11: board_edge_clearance 2, clearance 9 |
| nano-s7 | 35/64 | 29 | 206 | 95 | 2424.8 | 99.1 | 1.55 | 0.481 | 35.74 | 78: connectivity 14, silk_missing 35, unrouted 29 | 14: board_edge_clearance 2, clearance 10, silk_edge_clearance 2 |
| nano-s8 | 38/64 | 26 | 206 | 130 | 2756.8 | 87.4 | 1.37 | 0.424 | 37.95 | 67: connectivity 8, silk_missing 33, unrouted 26 | 21: board_edge_clearance 2, clearance 9, silk_edge_clearance 10 |
| nano-r2 | 42/72 | 30 | 226 | 123 | 2921.0 | 106.7 | 1.48 | 0.472 | 23.19 | 90: connectivity 9, silk_missing 51, unrouted 30 | 12: board_edge_clearance 3, clearance 9 |
| esp32c3-s1 | 11/11 | 0 | 61 | 27 | 207.3 | 12.3 | 1.12 | 0.202 | 1.55 | 0: — | 24: trace_width 24 |
| esp32c3-s2 | 11/11 | 0 | 61 | 31 | 242.4 | 12.8 | 1.16 | 0.210 | 1.82 | 0: — | 24: trace_width 24 |
| esp32c3-s3 | 11/11 | 0 | 61 | 33 | 207.1 | 17.5 | 1.59 | 0.287 | 1.83 | 0: — | 26: trace_width 26 |
| esp32c3-s4 | 11/11 | 0 | 61 | 31 | 221.0 | 15.7 | 1.43 | 0.257 | 1.55 | 0: — | 24: trace_width 24 |
| esp32c3-s5 | 11/11 | 0 | 61 | 32 | 217.6 | 14.5 | 1.32 | 0.238 | 1.64 | 0: — | 26: silk_edge_clearance 2, trace_width 24 |
| ewod-replay | 51/55 | 4 | 55 | 25 | 454.9 | 35.9 | 0.65 | 0.653 | 0.38 | 0: — | 160: annular_ring 55, clearance 50, trace_width 55 |
| sanity-straight | 20/20 | 0 | 20 | 20 | 1000.2 | 0.4 | 0.02 | 0.020 | 0.00 | 0: — | 0: — |
| qfp-plaza | 54/55 | 1 | 55 | 36 | 274.0 | 5.9 | 0.11 | 0.107 | 0.05 | 39: clearance 39 | 181: annular_ring 91, trace_width 90 |

## Not in v1

Differential pairs, length matching and impedance control; thermal design
beyond what appetite gives; component heights and 3D clearance
(`edge_mate.py` is not in the energy); blind and buried vias.

## Slices (proposed; logistics later)

1. Prototype, measurement only: 2 layers, 30–50 fixed pads, straight
   ratsnest with crossings; 4a seed, 4b beliefs, 4c relaxation with segment
   barrier and via beads. Report: legal crossing-free result; via count
   against the frozen grid table on the same ratsnest; whether the
   4b → 4a → 4c layer loop settles or oscillates; how often the swept
   collision check blocks steps; evaluations per second on CPU and Spark.
2. v1 on the bench boards with free parts, blocks, wire refinement, finish
   and exact DRC.
3. Algorithmic sub-clustering, kept if it beats v1.
4. Discrete moves beyond side flip and swaps (tree re-planning).
5. Physical layer terms beyond loop area (impedance, stitching).

## Slice 1 results

Reto 2026-10-21 run, d-pr-s1 worktree, CPU (Apple Silicon dev box, torch
2.13 CPU; no GPU/Spark was reachable — evals/s is CPU only).

Provisional parameters (fixed per the brief): length weight 1/mm; via
cost 1.0 mm wire-equivalent (= `vroute.Request.via_cost_mm`); one-sided
quadratic barrier on gap < 0, stiffness 20→400 ramped with linear width
growth 0→full over the first `grow_steps`; K = 16; linear temperature
anneal T = 2.0 → 0.3 in 4b; via sites every 2 mm; piece ≤ 2 mm; swept
check = orientation-sign flip on same-layer foreign pairs, halve ≤ 5×;
CEM α = 0.7, elite 25 %, Q floor 0.02; 3 outer rounds in the bench
(shorter than the spec's open loop — a bench-budget choice, reported).

Fixtures are `qfp_fanout` 2-layer (`sources="grid"`), fixed pads, no
placement. vroute numbers are `realize.realize` on the same IR —
context only; no frozen-table row is a 2-layer fixed-pad board.

| fixture | wires | unrouted | vias | wire mm | DRC findings (non-silk) |
|---|---|---|---|---|---|
| pr small (pps=6, n=12) | 12 | 6 | 2 | 60.6 | clearance 33, trace_width 49, annular_ring 14 |
| vroute same IR | 12 | 0 | 8 | 101.8 | clearance 12, trace_width 20, annular_ring 20 |
| pr main (pps=10, n=40) | 40 | 9 | 36 | 334.3 | clearance 190, trace_width 195, annular_ring 76, via_pad_keepout 1, via_via_keepout 2 |
| vroute same IR | 40 | 5 | 23 | 420.2 | clearance 13, trace_width 58, annular_ring 63 |

1. **Legal crossing-free result: no on both fixtures.** Unrouted wires
   are real (the seed is whole-wire-per-layer; every QFP pad pins its
   wire's last span to B.Cu, so the seed's B.Cu is congested and 4d
   moves — out of slice scope — are what would recover it). Clearance
   errors are seeded over-capacity claims the barrier could not relax
   away: first debt, main fixture — `portal 21 on B.Cu, 0.298 mm gap,
   1 wire through 0 lane(s)`. `trace_width` counts pad-neck checks too
   (vroute shows the same rule on the same IR).
2. **Vias/length vs vroute, same IR:** pr uses more vias (36 vs 23) and
   less wire (334 vs 420 mm) on main — expected: span-level layer choice
   buys F.Cu escapes cheaply; vroute pays wire to stay planar.
3. **Layer loop settles, not oscillates:** mean per-span belief entropy
   0.915 → 0.638 → 0.153 bits over 3 rounds (small), 0.816 → 0.207 (main).
4. **Swept collision blocks:** small 9.3 % of sample-steps needed ≥1
   halving; main 9.1 blocked sample-rejections per relax step (heavier —
   the seeded B.Cu crowding sits on the check; no step ever applied a
   crossing).
5. **Throughput (CPU):** 19.1 evals/s at N=103 spans (small); 2.2
   evals/s at N=361 (main). wall 9 s / 94 s. Spark not measured — no
   GPU/Spark host was reachable from the tree.
6. Parameters: listed above.

Reading: the approach produces measurable answers but slice-1 legality
is capped by two structural limits the numbers name — whole-wire seed
layers leave B.Cu over capacity (soft-capacity seeds exist but the
debt is reported, not solved), and span subdivision at seed granularity
makes crossing-free relaxation depend on where the via sites happened
to land. Both are 4d/4a-business for slice 2, not blockers for the
measurement this slice exists to produce.

## Slice 2 results

2026-10-23 run (pr-s2 worktree), CPU only (Apple Silicon dev box, torch
2.13 CPU). No GPU path was reachable from a worktree: the Spark lanes
need a deployed job type (`ssh_node`) and the sandbox lane is dark, so
evals/s below are CPU figures; the pairwise kernel (KeOps/Triton) is not
built. **vroute stays the production router: pr v1 beats it on no bench
board.**

What was built (`src/precis/pcb/pr/`, `v1.py` driver): part poses as 4c
variables with pads riding them (`state.DynGeom`), courtyard
separating-axis barrier, outline hold-in, rotation priced on the 90°/45°
tiers through `preferred.py` (`field.ROT_WELL_COEFFS`), authored IR
groups as a rigid block level then prolong, wire refinement 4 → 2 → 1 mm
with beliefs carried by arclength, bead–bead and net-blind via keep-out
terms, copper appetite as a per-wire width above the current floor
(off in the bench, `appetite_scale=0`), a 4d layer-move repair for the
sampler's same-layer crossings (`uncross.py`), §Finish (`finish.py`:
rotation/grid sugar kept only when legal and no worse, RDP wire
clean-up under the same rule, exact kernel DRC over copper and
placement, a resumable route state), and per-wire violation
attribution so a wire still in violation at finish is reported
unrouted with its depth instead of emitted as illegal copper.

Bench: `scripts/pcb-pr-bench` (`tests/fixtures/pcb/pr_bench.py`),
DB-free. Each board is placed once by the place job's own call
(`optimize` on the place-only schedule, seed = the board's) with the
field polish opted IN (`pr_bench.PLACE_FIELD_POLISH`; the job's own
default is off, `FIELD_POLISH_DEFAULT`, and the route bench's main
column opts in the same way) and routed by both routers from that
placement; pr may then move parts. **No declared planes** (Reto 2026-10-23, me507871: no
flood pours), so GND is a routed star net for both — the frozen grid
table's esp32c3 rows had F/B.Cu planes and are not comparable.
DRC is the exact kernel pass, silk rules dropped, errors/warns by rule.
"-GND" rows leave the ground net unrouted on purpose (counted as
unrouted) to show the rest.

Parameters (provisional, named per the brief): K = 8; stages
4 mm × 2 rounds (60 steps, 30 grow), 2 mm × 2 (80/40), 1 mm × 2 (80/40);
block level 2 rounds at 4 mm when the IR has a multi-member group (nano
only); length 1/mm; via 1.0 mm; barrier one-sided quadratic, stiffness
20 → 400 with width growth; courtyards at full stiffness, margin 0.02 mm;
copper margin 0.01 mm; part step ≤ 0.1 mm and ≤ 0.5° per relax step;
rotation well weight 1; belief T 2.0 → 0.3 over all rounds, pair cost
2 mm, crowd 0.6 mm; CEM α 0.7, elite 25 %; cleanup tolerance 0.005 mm;
demotion threshold 1e-3 mm² (≈ 0.03 mm depth).

| board | router | conns | unrouted | vias | wire mm | wall s | DRC s | DRC errors | DRC warns | travel mm | turn ° | off-90 | evals/s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nano | pr | 206 | 193 | 4 | 57.6 | 850.7 | 0.06 | 0: — | 21: board_edge_clearance 2, clearance 19 | 0.13 | 0.0 | 0.01 | 1.4 |
| nano | vroute | 206 | 125 | 62 | 1534.2 | 57.6 | 0.12 | 0: — | 20: board_edge_clearance 2, clearance 18 | 0.00 | 0.0 | 0.00 | 0.0 |
| esp32c3-s1 | pr | 61 | 56 | 0 | 19.9 | 387.8 | 0.01 | 0: — | 0: — | 0.15 | 0.0 | 0.00 | 1.2 |
| esp32c3-s1 | vroute | 61 | 25 | 14 | 341.9 | 13.6 | 0.05 | 0: — | 33: trace_width 33 | 0.00 | 0.0 | 0.00 | 0.0 |
| esp32c3-s2 | pr | 61 | 58 | 2 | 12.9 | 583.1 | 0.01 | 0: — | 3: clearance 1, courtyard_overlap 2 | 0.04 | 1.6 | 0.03 | 0.8 |
| esp32c3-s2 | vroute | 61 | 25 | 22 | 471.4 | 25.1 | 0.07 | 0: — | 39: trace_width 39 | 0.00 | 0.0 | 0.00 | 0.0 |
| esp32c3-s3 | pr | 61 | 54 | 2 | 27.0 | 502.9 | 0.01 | 0: — | 1: clearance 1 | 0.13 | 0.0 | 0.00 | 1.0 |
| esp32c3-s3 | vroute | 61 | 47 | 8 | 150.9 | 50.5 | 0.02 | 0: — | 1: trace_width 1 | 0.00 | 0.0 | 0.00 | 0.0 |
| esp32c3-s4 | pr | 61 | 55 | 2 | 23.1 | 532.9 | 0.01 | 0: — | 0: — | 0.03 | 0.0 | 0.00 | 0.9 |
| esp32c3-s4 | vroute | 61 | 25 | 4 | 426.0 | 13.5 | 0.07 | 0: — | 23: trace_width 23 | 0.00 | 0.0 | 0.00 | 0.0 |
| esp32c3-s5 | pr | 61 | 58 | 8 | 31.7 | 387.8 | 0.01 | 0: — | 0: — | 0.06 | 0.0 | 0.00 | 1.3 |
| esp32c3-s5 | vroute | 61 | 25 | 10 | 374.1 | 30.7 | 0.03 | 0: — | 29: trace_width 29 | 0.00 | 0.0 | 0.00 | 0.0 |
| qfp-plaza | pr | 55 | 39 | 0 | 56.7 | 89.8 | 0.03 | 79: clearance 79 | 184: annular_ring 55, clearance 2, courtyard_overlap 88, trace_width 39 | 0.00 | 0.0 | 0.00 | 10.8 |
| qfp-plaza | vroute | 55 | 2 | 36 | 266.2 | 20.6 | 0.05 | 78: clearance 78 | 268: annular_ring 91, courtyard_overlap 88, trace_width 89 | 0.00 | 0.0 | 0.00 | 0.0 |
| nano -GND | pr | 206 | 193 | 4 | 57.6 | 817.0 | 0.05 | 0: — | 21: board_edge_clearance 2, clearance 19 | 0.13 | 0.0 | 0.01 | 1.3 |
| nano -GND | vroute | 206 | 128 | 56 | 1489.3 | 54.4 | 0.17 | 0: — | 20: board_edge_clearance 2, clearance 18 | 0.00 | 0.0 | 0.00 | 0.0 |
| esp32c3-s1 -GND | pr | 61 | 53 | 10 | 54.0 | 155.8 | 0.01 | 0: — | 0: — | 0.48 | 0.0 | 0.00 | 3.2 |
| esp32c3-s1 -GND | vroute | 61 | 25 | 14 | 341.5 | 7.4 | 0.05 | 0: — | 33: trace_width 33 | 0.00 | 0.0 | 0.00 | 0.0 |
| esp32c3-s1 seed-layers | pr | 61 | 52 | 0 | 33.2 | 212.5 | 0.01 | 0: — | 0: — | 0.02 | 0.0 | 0.00 | 2.3 |
| qfp-plaza seed-layers | pr | 55 | 40 | 0 | 71.2 | 49.2 | 0.03 | 78: clearance 78 | 160: annular_ring 55, clearance 2, courtyard_overlap 88, trace_width 15 | 0.00 | 0.0 | 0.00 | 61.5 |

(Columns are `pr_bench.render_table`'s. "seed-layers" rows: 4b off,
every sample keeps the 4a seed's layers, K = 2 — 4c alone on the seed.
"off-90" is the fraction of the movable parts written off the 90°
grid; the placement gate's cap is `MAX_OFF90_FRAC` = 0.10. qfp-plaza's
78 clearance errors are the fixture's own pad–pad gaps, present before
either router runs (pr adds one at 0.096 mm against the 0.100 mm fab
floor — the class clearance there is 0.099); esp32c3's vroute
`trace_width` warns are pad-neck checks.)

Where the connections go, per stage (4a seed alone, then after the
full loop):

| board | 4a seed leaves unrouted | reasons | pr final unrouted (of which demoted at finish) |
|---|---|---|---|
| nano | 126 / 206 (GND 55) | no path on any layer 126 | 193 (67) |
| esp32c3-s1..s3 | 0 / 61 | — | 56, 58, 54 (56, 58, 54) |
| esp32c3-s4 | 8 / 61 (GND 8) | ripped 7, no path 1 | 55 (55) |
| esp32c3-s5 | 17 / 61 (GND 10) | ripped 13, no path 4 | 58 (58) |
| qfp-plaza | 36 / 55 | no path 27, ripped 9 | 39 (14) |

Readings:

1. **Legal output, little of it.** Every pr row is DRC-clean on copper
   (0 errors; qfp-plaza's are the fixture's) because the finish demotes
   every wire still in violation. That demotion is what the numbers
   measure: on esp32c3 the 4a seed produces a polyline for all 61
   connections, and 54–58 of them are lost afterwards. On
   nano and qfp-plaza the seed itself fails most connections ("no path
   on any layer": the net-domain funnel over inflated obstacles finds no
   corridor where vroute's A* with escape stubs does).
2. **The loss is baked in at 4a, and 4b adds to it.** The "seed-layers"
   row (4b off) still loses 52 of 61 on esp32c3-s1: the seed's own
   layer assignment carries ~235 same-layer crossings per sample
   (`crossings` 470 over K = 2, none repairable — the end spans are
   pinned to their pad's layer). Those are the slice-1 "soft-capacity
   debt": a wire that fits no capacity-free portal is committed through a
   saturated one, and `seed.attempt` skips the crossing check on that
   pass, so "all 61 seeded" means "61 polylines, ~235 crossings", not a
   planar seed. 4b then roughly doubles it: with sampling on, crossings
   in the K = 8 samples run 800–5,500 per round before the layer-move
   repair and 150–2,400 after. Relaxation cannot undo a crossing of two
   polylines (moving a vertex across the other wire crosses the
   neighbouring piece, which the swept check rightly refuses), and with
   that many crossings the swept check blocks ~7 of 8 samples per step
   (`blocked` 6.9; 1.7 with 4b off), so geometry barely moves. The
   spec's inner loop 4b → 4a (re-insert changed spans) → 4c is
   implemented only for the best sample between rounds; per-sample
   crossing-free re-insertion needs per-sample layouts, which breaks the
   (K, N, 2) batching. The "-GND" rows show the ground star is not the
   cause on nano (identical pr numbers: the seed already fails every GND
   wire) and halves the crossings on esp32c3-s1 without changing the
   outcome; on qfp-plaza with 4b off the seed is nearly planar (6
   crossings) and only 5 wires are demoted, but the seed itself left 36
   unrouted.
3. **Free parts behave.** Parts move 0.03–0.5 mm from the job
   placement; every written rotation is a 45°-grid value (the placer's
   D5/Q7 write policy: the nearest 90° unless a 45° pose beats it by
   `ROT_TILT_MIN_GAIN`), so the off-90 fraction is 0.00–0.03 against the
   gate's 0.10 cap (one 45° tilt earned its keep on nano and one on
   esp32c3-s2); the grid sugar moved 9–22 parts per board under the
   legality rule; placement DRC adds at most 2 courtyard warns. The block
   level ran on nano (authored groups) and prolonged rigidly.
4. **Two defects found and fixed on the way, both affecting slice 1's
   numbers too:** an unrouted wire was relaxed as a straight phantom line
   between its pads (it now joins no pair), and an authored outline
   ring's repeated closing vertex gave the edge term a zero-length edge
   so every point measured "at the edge" (now stripped; the kernel skips
   such edges). The belief pair table also missed crossing spans whose
   endpoints were far apart (tested endpoint proximity only); fixed, with
   no measurable effect on the crossing counts above. The review of the
   first bench found the 4a re-funnel silently skipped at every 4 → 2 and
   2 → 1 mm refinement boundary (piece counts no longer matched); it is
   carried by arclength now, which moved esp32c3-s1 from 58 to 56
   unrouted and nothing else materially — the re-funnel was not the
   crossing debt.
5. **Throughput (CPU):** 0.8–1.4 evals/s on nano/esp32c3 at K = 8 with
   255–860 spans and 30–85 free parts (pair tables 270k piece pairs on
   nano), 11 evals/s on qfp-plaza (fixed parts). Wall 6–14 min per
   board against vroute's 13–58 s; the seed alone is 8–85 s.

What slice 3+ needs before this can be re-benched against vroute:
(a) a 4a seed that is planar per layer when it commits — soft-capacity
commits must take the crossing check, and a wire that fits nowhere must
spread over layers (via sites inside the seed) rather than be committed
through a saturated portal; (b) per-sample 4a re-insertion so a sampled
layer change is crossing-free on its new layer (the spec's inner loop),
or a sampler that moves whole runs under a planarity check; (c) a seed
that reaches vroute's corridors on fine pitch (escape stubs from pads,
or seeding from vroute's own paths); (d) tree re-planning for the star
hub nets (slice 4). The engine, bench and finish are in place for that
work; nothing is wired into `pcb_route`.
