---
status: draft
title: "pcb: exact vector (gridless) routing — one shared clearance kernel, a tangent-graph single-net router, then a topological global router"
prio: high
model: opus
pillar: 3d-design
---

# pcb vector router

Source: Reto, 2026-10-16, on gr489923 ("why not just vectors and spacing…
allows curves and proper diagonals"; se already uses exact math objects),
plus three same-day additions: "linear algebra magic", "GPU stuff", and the
EWOD HV builder's per-(net, layer) clearance need. Reto's rulings on the
first round of open questions: §Decisions (Reto 2026-10-16); two remain
open there. Research:
`perplexity-research:491905` (topological routing, SURF/TopoR, MCF on a
CDT), `491939` (shape-based routers, tangent graphs, conservative arcs,
shapely vs Clipper2), `491941` (continuous optimisation, SDFs, GPU); read
with `get(kind='perplexity-research', id='<slug>', args={'no_fetch': True})`.

## Problem and evidence

The maze (`maze.OccupancyGrid`, pitch = clearance × 2/3, about 0.1 mm, plus
one cell of query slack, `maze.py` module docstring) gets clearance right by
construction and loses every passage narrower than about two cells.

| Case | Fact | Source |
|---|---|---|
| taz15 CN4 | 0.3 × 1.55 mm pads, 0.5 mm pitch, 0.3 mm track, 0.152 mm clearance: 0.048 mm lateral slack, under one cell. 8 nets (CN4_40/42/56/58/60/67/74, MCU_BOOT) fail `no_path: walled in`. The EasyEDA source routes exactly these. | gr489923 |
| taz15-blank | 139/151 without the stub; 149/151 with e8753d909 (exact-geometry escape stub, `OccupancyGrid.escape_stub`, `realize._route_with_escape_stubs`). Last 2 are `plane_drop_blocked` (GND, PV+). | gr489923, e8753d909 |
| nano seed 1 | Stub as landed: routed/failed 36/28, silk_missing 39, clearance warns 11, against floors 35/29, 36, 9. It fired 63 times, mostly on congestion failures; the new warns are nowhere near a stub. Gated to pads with no passable cell: 34/30, 37, 9. | gr491588 |
| e8753d909 | Reverted in 4949f7ee6 (router-lane trio, with 2bf9dc75e and f97455a37); held. Backup branch `rlf-full-backup`. | git log, gr491588 |
| ewod replay | The tile plan (`globalplan.py`) cannot express the braid an escape needs inside one tile; every plan-guided variant ties or loses to plain 51/55. | `pcb-global-plan-router.md` slices 0–1b |
| HV pairs | DRC grades `|V_a − V_b|` pairwise (`drc.check_clearance`); the router cannot (one claim radius per net). "Still open: the ROUTER half." | `pcb-missing-constraint-classes.md` §E-1 |
| EWOD HV | `working_voltage_v=250` sets a 0.4 mm ERROR floor on every layer (`rules.resolve_net_rules` → `NetRules.min_clearance_mm`). Physics needs 0.4 mm on B.Cu only; F.Cu pads are parylene-coated and the HV is plate-to-pixel. The array's own F.Cu plaza stubs (`track[ARR1_R0C2] ↔ pad[ARR1/R1C2]`) are refused. | EWOD HV builder, 2026-10-16 |

Two diagnoses follow:

1. e8753d909's geometry was right and its trigger was wrong. It checked pads
   and tracks exactly and everything else on cells, and it ran inside the
   pass, so every extra stub moved later nets. A replacement must run where
   it cannot change copper the maze already placed, and must check every
   object kind exactly.
2. A clearance rule today is one number per net. The physics is a function
   of (object a, object b, layer): voltage difference, coating, same-net
   relation, object kinds. The grid can only stamp a per-net radius, so the
   rule model and the router change together.

Two smaller defects the kernel removes:

- `drc._arc_points` flattens arcs into 15° chords. Chords lie inside the arc,
  so the outer side of an arc track reads up to `R(1 − cos 7.5°)` closer to
  its centre than it is: 4 µm at R = 0.5 mm, 43 µm at R = 5 mm. That is the
  same order as CN4's whole slack. Shapely round buffers err inward the same
  way (`491939`: GEOS chords are inscribed). `drc._BUFFER_QUAD_SEGS` = 64
  makes that error negligible for pads; the arc flattening does not.
- The maze, `_plane_fanout`, `_drop_via_site`, the stitcher and DRC each
  answer "is this copper legal here" with their own predicate
  (`OccupancyGrid.disk_is_free`, `via_clears_pads`, `realize._via_clears_vias`,
  `_clears_foreign_pours`, `drc.clearance_pairs_indexed`). They disagree at
  the margin, and the disagreement shows up as post-route DRC strips and
  `clearance` warns.

## Goals

- One exact clearance kernel. Routing, via-site search, stitching and DRC ask
  it the same question and get the same answer.
- A clearance rule keyed by (class pair, layer), with voltage, coating and
  same-net relations resolved in one function.
- A gridless single-net router that leaves fine-pitch pads the way the source
  board does: taz15-blank ≥ 149/151 with no loss on any floor.
- Then a topological global router that replaces the tile plan, with
  bundling and stackup costs at the topology level.
- Retire the grid maze once vector routing matches or beats it (slice 10,
  Decision 1). Until then the maze is the first pass during transition only.
- Geometry stays lines and circular arcs (Gerber G01/G02/G03, the
  `realize.RealizedTrack` segment shape). No splines, no beziers.

## Non-goals

- Push-and-shove interactive routing (KiCad PNS). Batch only.
- Differential pairs and impedance control (no engine support exists).
- Any-angle output in slices 0–9 (a per-PCB option is slice 11, later).
- A GPU requirement. Every path has a CPU implementation that tests run on.
- Changing the placer, except where the topology router's min-cut witness
  feeds `optimize.anneal_route_loop` (already specced in
  `pcb-global-plan-router.md` slice 4).

## Invariants this item keeps

- **Producer and checker stay independent** (`realize.py` module docstring,
  `drc.py` "reference oracle"). The kernel is the shared *definition* — rule
  resolution, primitive shapes, the accelerated distance engine. The
  verdict that guards it stays the dependency-free O(n²) oracle
  (`drc.clearance_violations_naive`), extended to arcs and rectangular
  pads so it covers what the kernel covers. `connectivity.net_islands` stays
  independent of every producer.
- **Never commit a worse board** (`pcb_route` docstring, gr480010): unchanged.
- **Body chunks rule** does not apply here; `pcb_copper` keeps its
  DELETE + INSERT cascade.
- **Fail legibly**: every vector-router failure carries an `UnroutedReason`
  kind and the witnesses, as the maze does.

## Decisions (Reto 2026-10-16)

| # | Question | Ruling |
|---|---|---|
| 1 | Maze long-term? | **Retire it** once vector routing is complete. Until then it is the first pass during transition only; slice 10 retires it against explicit exit criteria. |
| 2 | Default angle | **45° plus arc fillets.** A per-PCB any-angle option comes later (slice 11), not in slices 0–10. |
| 3 | Arcs by default | **Only where hugging a round obstacle forces one.** |
| 4 | Topology router vs stackup slices | **In parallel.** Stackup 0–7 as specced, with their costs in the shared route cost; only stackup slice 8 moves onto `topo` portals. |
| 5 | Clearance rule model | **Per-layer table and class-pair matrix**, class-pair rules winning; region-scoped rules later with derived regions. |
| 6 | Margin δ | **5 µm** ("seems small but yes"). |
| 9 | Polygon engine | **GEOS now**; Clipper2 only if profiling shows buffer/union needs it. |
| 10 | GPU | **No GPU now.** JAX behind a job with a CPU fallback only if slice G shows ≥ 3× end-to-end. |
| 11 | Topology solver | **PathFinder on the CDT first** as the baseline (slice 6a), then the LP (slice 6b). |
| 12 | Kernel shared with DRC | **One shared kernel**; the independent O(n²) oracle stays and gains arcs and rectangular pads. |
| 7 | Arcs in the kernel | **Analytic, no polygonisation in the kernel.** Remaining polygons (CDT input, pour booleans, views) are drawn outward at **5 µm** sagitta. |
| 8 | Runtime budget | **Quality first.** A slower route is accepted when it realizes more or has fewer DRC findings; the route job's wall deadline is the only hard cap; runtime is reported per board size; the per-slice figures only flag profiling. |

### Notes on 7 and 8

**7. Why not true arcs — is it a Gerber limitation?** No. Gerber (G02/G03)
and KiCad (`arc` track items) carry true circular arcs, and so do
`RealizedTrack` segments and `pcb_copper.geom`, so the output is real arcs.
The polygon tolerance was only GEOS's internal stand-in for curved shapes
during buffer, union and graph construction. The design can drop it from
the kernel entirely: Disc, Capsule and ArcCapsule pairs have closed-form
distances (point–arc, segment–arc, arc–arc), a polygon is its edges, and an
inflated obstacle stays the same primitive with a larger radius, so the
verdict, configuration space and the tangent graph are all analytic (step 1,
step 2). Polygons remain where a consumer only accepts polygons: step 3's
CDT input (`shapely.constrained_delaunay_triangles`), the pour booleans in
`planes.py`, and rendered views. There they are always outward
(circumscribed), so they can narrow a corridor by at most the tolerance and
never open a false one. The remaining question is that tolerance: lean 5 µm
sagitta (5 segments per quadrant at R = 0.3 mm, 11 at R = 2 mm).

**8. Is the runtime budget a limitation; should it relax?** It is a design
target, not a correctness limit: clearance and connectivity hold however
long a route takes. Proposal: (1) quality first — a slower route is
accepted whenever it realizes more nets or has fewer DRC errors or warns,
and a runtime rise with equal quality is reported, not refused; (2) the job
wall is the only hard cap (a `deadline_s`-style bound on `pcb_route`,
sized in slice 0, e.g. 30 min), and hitting it keeps the best result so far
under the never-worse ratchet; (3) runtime is reported per board size
(seconds per net and per connection, slice 0's table), so a regression
reads against the board's size rather than one global number. The
per-slice figures (post-pass ≤ 10 % of maze, `topo` ≤ 2× maze, LP ≤ 30 s)
stay as targets that flag a slice for profiling.

## Design

### Step 1 — the clearance kernel (`src/precis/pcb/clearance.py`, new)

**Primitives.** Every copper and non-copper object is one of four exact
shapes, built once per board from the same `to_gerber_model` dict DRC reads:

| Primitive | Fields | Covers |
|---|---|---|
| `Disc` | centre, r | round pad, via land, drill (as hole), fiducial keep-out |
| `Capsule` | a, b, r | straight track segment, obround pad |
| `ArcCapsule` | centre, R, θ0, θ1, cw, r | arc track segment |
| `Poly` | shapely Polygon (exact for rect and polygon pads, pours, courtyards, silk boxes, outline, mask openings) | everything with straight edges |

Each carries an `Owner`: net (or none), kind (`pad`, `track`, `via`, `pour`,
`hole`, `silk`, `courtyard`, `edge`, `mask`, `keepout`), layer set (a via
carries its span, as `RealizedVia` does), footprint id, coated flag.

**Distances are analytic.** Disc/Capsule/ArcCapsule pairs use closed forms
(point–segment, segment–segment, point–arc, segment–arc, arc–arc with the
candidate sets listed in `491941` §3). A `Poly` is its edges: Poly–Disc and
Poly–Capsule use shapely `distance` against the centre point or segment
minus the radius; Poly–ArcCapsule is the minimum segment–arc distance over
the polygon's edges, plus a containment test. No pair is ever flattened, so
the verdict carries no polygon tolerance. Polygonisation exists only where
a consumer needs polygons — step 3's CDT input, `planes.py` pour booleans,
and rendered views — and is always outward: a circle of radius R becomes
the circumscribed polygon with vertex radius `(R + δ)/cos(π/(4q))`
(`491939`, "over-buffer before GEOS"); an arc track's outer side the same.

**Index.** One shapely 2 `STRtree` per (layer, owner kind group) over each
primitive's bounding geometry, queried with `predicate='dwithin'` at the
largest requirement that layer can produce. Vectorised array queries and
numpy/numba distance kernels; no Python loop over obstacles on the hot path. `491939`
measures nothing at our scale that favours Clipper2 over GEOS; Clipper2 is
a fallback only if profiling shows buffer/union dominating (Decision 9).

**Rule resolution: one function, pairwise and per layer.**

```python
def required(a: Owner, b: Owner, layer: str, ctx: RuleContext) -> Requirement
# Requirement(error_mm, warn_mm, metric)  metric ∈ {"copper", "hole_wall", "none"}
```

Resolution order, first match wins for the relation, then `max()` over the
applicable floors:

1. Same net: copper merges (`none`), except via↔via → `hole_wall` ≥
   `JLC_VIA_HOLE_TO_HOLE_MM` (gr489914 ruling (a), `drc.check_via_via_keepout`)
   and via↔own pad → the net-blind via-in-pad keep-out
   (`maze.OccupancyGrid.via_clears_pads`'s rule, moved here).
2. Same footprint, coated board (`gerber.declares_parylene_coat`): the
   intra-footprint class floor is a WARN (62577fe75, d69c223d3).
3. Class-pair, per-layer table (new, below).
4. Voltage: `capabilities.conductor_spacing_mm(|V_a − V_b|, layer=outer|inner,
   coated=…)`, half-annotated pairs → `voltage_spacing_unknown` as today.
5. Per-net floors from `rules.resolve_net_rules` (fab floor, class
   `clearance_mm`, `min_clearance_mm`), now resolved per layer.
6. Kind rules: board edge (routed vs V-cut), NPTH, silk-to-pad, courtyard,
   mask — the thresholds `drc.check_*` already read from `capabilities`.

`error_mm` is the `jlc_min`/hard-floor tier, `warn_mm` the `house_default`
tier (`drc.py` "two-tier margin"). **Routers target `warn_mm`**, so vector
copper adds no `clearance` warns by construction; DRC grades both tiers.

**Per-(class, layer) clearance (the EWOD HV need).** Three keys in
`pcb_net_classes.rules` (jsonb; no migration):

```json
{"working_voltage_v": 250,
 "clearance_by_layer": {"F.Cu": {"min_clearance_mm": 0.152}, "B.Cu": {"min_clearance_mm": 0.4}},
 "pair_rules": [{"with": "ewod_pixel", "layers": ["F.Cu"], "min_clearance_mm": 0.152}]}
```

`clearance_by_layer` overrides the class's per-net floor on that layer;
`pair_rules` sets a floor for a class pair on listed layers (the matrix
form, sparse). `working_voltage_v` keeps deriving a floor, but per layer:
outer/inner column from the stackup, coated column on layers the coat
covers. Decision 5: both forms, `pair_rules` winning over
`clearance_by_layer`. Region-scoped
rules wait for derived regions (`pcb-stackup-regions.md` slice 4).

**Who calls it.**

| Caller | Today | After |
|---|---|---|
| `drc.check_clearance` | `clearance_pairs_indexed` + per-net `max()` + voltage fold | kernel pairs + `required()`; findings and `finding_key` identical |
| `drc.check_via_via_keepout`, `check_via_pad_keepout`, `check_npth_clearance`, `check_board_edge_clearance` | own thresholds | `required()`; geometry from kernel |
| `realize._drop_via_site`, `_via_clears_vias`, `_clears_foreign_pours`, `_plane_fanout`, `_stitch_plane_fragments` stages 1–3 | grid disks + local shapely | `kernel.clears(prim, owner, layer)` |
| `maze.OccupancyGrid` | per-net `extra_mm` | per-(net, layer) `extra_mm`; pairwise rules stay DRC-only on the grid (cannot be stamped), enforced by the vector router and the post-route strip |
| vector router (step 2) | — | configuration space from `required()` |
| `drc.clearance_violations_naive` | capsules/circles, no arcs, no pads | stays independent; gains arcs and rect pads; asserted equal to the kernel on randomized boards |

### Step 2 — single-net vector router (`src/precis/pcb/vroute.py`, new)

**Configuration space.** For net n of width w on layer L, every foreign
obstacle o becomes `o ⊕ disc(w/2 + required(n, o, L).warn_mm + δ)`, δ =
5 µm (Decision 6) — a Minkowski sum, kept analytic: an inflated Disc,
Capsule or ArcCapsule is the same primitive with radius `r + ρ`, an inflated
Poly is its offset edges joined by vertex arcs of radius ρ. The track
centreline must stay outside every inflated obstacle; "outside" is the
analytic distance test, so overlapping obstacles need no union. Same-net
objects are not obstacles except the via rules in step 1.

**Graph.** A lazy tangent visibility graph (Liu & Arimoto 1992, doi
10.1177/027836499201100409; lazy expansion as in Freerouting's rooms,
`491939`): nodes are terminals and tangent points on the inflated
boundaries; edges are free bitangent segments plus boundary arcs between
tangent points. Edges are generated when A* expands a node and tested
against the index. Euclidean A* is admissible. Any-angle by construction.

**45° mode.** `drc.check_octilinear` is a house ERROR rule (every drawn line
at a multiple of 45°, arcs exempt; user requirement 2026-08-31). Default
output therefore stays octilinear: each free-angle edge is replaced by the
shortest two-segment octilinear connector that stays outside the
C-space, else that edge is dropped; corners may take a tangent arc fillet
(arcs are exempt). Decision 2: 45° plus arc fillets is the default; a
per-PCB any-angle option is slice 11, after the maze retires.

**Layers and vias.** Per-layer graphs joined by via edges at sampled sites.
Sites: the pad-end escape points (one per rectangular pad end, on the pad's
long axis, first point clear of the pad's own keep-out), the
`_drop_via_site` candidates, and a hex lattice at the via envelope inside a
window around each terminal, each filtered by `kernel.clears` for the full
via span. Via cost `maze.VIA_COST_MM`, under-body surcharge as the maze.
Layer costs and direction penalties come from the shared route cost (below).

**Output.** `realize.RealizedTrack` with `line`/`arc` segments and
`RealizedVia` with a layer span — the existing shapes, so gerber, DRC,
`pcb_copper` and `epro_write` need no change. Each vector path is stamped
back into the grid with `realize._stamp_realized_track` so a later maze
pass sees it.

**Where it runs (replaces e8753d909).** In `realize._realize_maze`, after the
best pass is chosen and before planes and stitching:

1. Take the failed segments whose `UnroutedReason` is `no_path` with an
   endpoint walled in (slice 3), then all remaining failures (slice 4).
2. Route each with the vector router against the kernel built from the
   best pass's copper. Nothing the maze routed moves.
3. Accept a path only if the kernel finds no violation against every object
   kind, silk and courtyard included.

Because it runs on the frozen best pass, it cannot reorder or displace maze
copper, which is what cost e8753d909 the nano floors. What it can still
change is silk and pours downstream of the new copper; the slice gates
measure exactly that.

**Shared route cost.** Per-layer multiplier (In1 at about 2–3×,
`pcb-stackup-regions.md` ruling), direction penalty per stackup `direction`
key, via cost, bundle discount. One module (`src/precis/pcb/routecost.py`,
new, or a section of `cost.py`) read by `maze._step_penalty`,
`globalplan`, and `vroute`, so stackup slices 3 and 7 land once for all
routers.

### Step 3 — topological global router (`src/precis/pcb/topo.py`, new)

The tile graph's measured limit is the intra-tile braid (`pcb-global-plan-router.md`
slice 1). A constrained Delaunay triangulation of free space has no tiles:
every portal is a gap between two real obstacles.

1. **Region graph.** Per layer, `shapely.constrained_delaunay_triangles`
   (shapely 2.1, GEOS 3.13 in this venv; no new dependency) over the
   inflated obstacle boundaries. Dual graph: one node per free triangle,
   one edge per passable primal edge. Via edges join faces on different
   layers at the step 2 via sites.
2. **Portal capacity, physical.** `Σ (w_k + s_k) ≤ L_e` over nets crossing
   primal edge e, with `s_k` from `required()` for the neighbours on each
   side (Liu et al., D-PCDT, doi 10.1109/TCAD.2008.2009154).
3. **Topology assignment as an LP/ILP.** Path-based unsplittable
   multicommodity flow: per two-pin branch, 3–20 candidate homotopy classes
   (k-shortest distinct triangle sleeves on the dual, plus the vector
   router's own path); one binary per (branch, class); portal capacities and
   tile via capacities as linear rows; violated cut inequalities added
   lazily by min-cut separation. Solve the LP relaxation with HiGHS
   (`scipy.optimize.linprog`/`milp`; scipy 1.18 here), round the
   low-conflict branches, exact-ILP the residual saturated component
   (`491905` §3). Pin-swap groups stay inside it as "any pad of the group"
   sinks (`pinswap._hungarian`, as `globalplan.plan_routes` does).
4. **Ribbon order.** At congested triangles only, add left-to-right order
   variables per portal and the outerplanar non-crossing constraint per
   triangle (`491905` §2.4). Bundles are ribbons: nets of bundle-compatible
   classes sharing a portal sequence get the bundle discount.
5. **Geometric embedding.** Per ribbon, funnel string-pulling in its
   sleeve gives the shortest path in its homotopy class; obstacles'
   inflated boundaries supply tangent arcs; ribbon members are offset by
   `w + s` in portal order (SURF's spokes, Dai/Kong/Sato DAC 1991, doi
   10.1145/127601.127623); 45° conversion per step 2. A continuous cleanup
   (below) equalises spacing. The kernel verifies.
6. **Feedback.** An embedding failure becomes a no-good cut (this branch
   set, this portal order, these classes) and the LP re-solves. An
   infeasible LP yields the min-cut witness for `ConflictDigest`, the
   expansion moves and the LLM menu (`pcb-global-plan-router.md`).

Report four counts separately, never one: topologically connected,
capacity-feasible, geometrically embedded, kernel-clean (`491905`, last
section; TopoR's "100 %" counts the first).

Storage: the ribbon (sleeve, portal orders, via faces) goes in
`pcb_routes.meta.ribbon` (jsonb). `pcb_routes.topology` already holds the
sketch's per-segment side choice and is not reused. No migration.

### Step 4 — arcs, teardrops, length matching (optional)

Arcs stay where hugging a round obstacle forces them (Decision 3);
decorative corner arcs are not planned. Teardrops at
pad and via joins as derived copper (`epro` round-trip drops teardrops
today; EasyEDA regenerates them); length matching as serpentine insertion
in a ribbon's free length. Specced when step 3 has landed.

## Better maths: what each candidate buys

Reto asked whether vector maths can replace graph search. Verdict per
candidate (sources `491939`, `491941`):

| Candidate | Feasible | Exact | Runtime at our size | Verdict | Where |
|---|---|---|---|---|---|
| Configuration space by Minkowski sum | yes | yes analytically: an inflated Disc/Capsule/ArcCapsule is the same primitive with a larger radius, an inflated polygon is offset edges plus vertex arcs; polygonised (`shapely.buffer`, outward `(R+δ)/cos(π/4q)`) only where step 3's CDT and the pours need polygons | analytic inflation is O(n); buffer of a few thousand primitives: ms to tens of ms | **adopt**; it is step 2's obstacle model | step 1/2/3 |
| Per-query exact checks | yes | yes | the verdict path; STRtree prunes to tens of candidates | **keep** as the source of truth | step 1 |
| Signed distance, exact analytic (point/segment/arc/polygon) | yes | yes | the kernel's distance functions are already this | **adopt as the kernel's API** (`distance(prim, prim)`, `gradient`) | step 1 |
| Signed distance, sampled grid | yes | no — resolution error is the maze's problem again | fast | **reject** for legality; at most a cost heat map | — |
| Gradient path relaxation (elastic band, CHOMP) | yes for a seeded path | no completeness; sampled waypoints tunnel | seconds | **complement**: smoothing only, never routing | step 3 cleanup |
| Sequential convex optimisation (TrajOpt-style; linearised exact distances as inequality rows, trust region, QP per iteration via HiGHS QP) | yes, fixed topology | constraints exact at convergence; final kernel check authoritative | per ribbon or cluster: small QPs, ms each | **adopt for step 3.5** (ribbon spacing, equal clearance, bend/length cost); many nets as one sparse problem only per congested cluster | step 3 |
| LP/ILP multicommodity flow on the CDT | yes | integral solution exact w.r.t. capacities; cuts necessary not sufficient | LP: s to tens of s at 300–600 commodities (`491905`, engineering estimate); HiGHS warm starts | **adopt**; it replaces PathFinder-on-tiles for topology | step 3 |
| Spectral / Laplacian eigen methods | for netlist partition or placement init (NS-Place uses it) | no geometry, no obstacles, no clearance | cheap | **reject for routing**: eigenvectors encode connectivity smoothness, not free space; legal copper needs obstacles and integrality. Possible placer seed, out of scope | — |

**What replaces graph search and what complements it.** Graph search
stays for *topology*: the LP picks among homotopy classes that a graph
search (k-shortest sleeves on the CDT dual, A* on the tangent graph)
generates. Continuous maths cannot change a homotopy class, add or remove a
via, or pass one wire through another (`491941` §6), so it never routes
alone. It replaces the maze's *geometry* (exact C-space instead of cells)
and the rubber band's *spacing step* (SCP instead of SURF's spoke
heuristics). The grid maze and the tile plan are what this item retires,
not graph search.

## GPU: where it helps

**Execution model today.** `pcb_route` and `pcb_place` are
`COMPATIBLE_EXECUTORS = {"job_inproc"}`, `REQUIRES = frozenset()`: the worker
process runs them, never inline in an MCP call. The GPU jobs that exist
(`struct_relax`) use `ssh_node` with `REQUIRES = {"has_gpaw"}`
(`executors.EXECUTOR_PROVIDES`), pin to the GB10 node, and run a container
with `--gpus all` / CDI. A GPU pcb path would follow that shape: a capability
(`has_pcb_gpu`), a compute-node executor, a container or extra carrying
the GPU stack, and the same job writing the same rows. The CPU path stays
the default and the only one tests need.

| Use | Evidence | Pays off when | Verdict |
|---|---|---|---|
| Batched exact distances (feeds the SCP optimiser) | Exact polygonal SDF on GPU: 0.6 ms forward, 2.0–2.2 ms with gradients, flat to 100 × 100; CPU is 49× faster at batch 1, GPU 93× faster at 10⁴ pairs (`491941` §4) | ≳ 10⁶ primitive-pair evaluations per iteration, device-resident | Our boards: after STRtree culling, ≤ 151 nets × ~20 segments × ~30 neighbours ≈ 10⁵ pairs per iteration — CPU numpy/numba range (estimate, to be measured in slice G). **Not now.** |
| All nets' polylines as one batched optimisation (penalty / augmented Lagrangian, CPU exact check after) | No PCB-scale demonstration found (`491941` "Final assessment") | many restarts × large boards | **Not now**; revisit after slice 7 shows SCP cleanup is the time sink |
| GPU LP (cuOpt, cuPDLP) vs HiGHS | cuPDLP has ~1 s fixed overhead; GPU wins at ≥10⁵–10⁷ nonzeros; HiGHS simplex warm-starts our repeated LPs (`491941` §5) | never at a few thousand variables | **No**: HiGHS on CPU |
| Many-restart / evolutionary search (placement + route seeds) | Cypress GPU PCB placement: up to 492× vs CPU placers (doi 10.1145/3698364.3705346); routing stages gain 2.5–2.9× end-to-end at best (FastGR, GAMER, TRouter) | the placer's cost is tensorised, ≥ 32 restarts | **Most plausible GPU use**, but it is a placer item, not this one |
| Small boards (nano, esp32c3) | kernel launch 5–15 µs each, JIT compile > solve | — | CPU always |

GB10's unified memory removes host↔device copies, which lowers the
crossover, but launch overhead and compile time remain, and our per-iteration
work is about 10× below where the measured kernels win. Decision 10: no
GPU code in slices 0–10; slice G benchmarks the SCP cleanup on CPU vs GPU
on taz15 once that code exists, and only a ≥ 3× end-to-end gain starts JAX
work behind a job with a CPU fallback.

## Interaction with other items

**`pcb-stackup-regions.md`.** Its slices assume the grid in four places:
slice 1 (pre-claim drop-via sites on the grid), slice 3
(`maze.preferred_directions` reads the stackup `direction`), slice 7
(bundling discount in the maze step loop), slice 8 (ground-severance cost in
`globalplan.plan_routes`). Rule: stackup slices 0–6 proceed unchanged; slices
3 and 7 put their costs in the shared route cost (step 2), so they land in
the maze now and in `vroute`/`topo` without re-work; slice 1's pre-claim
uses `kernel.clears` for site legality and the grid for the claim; slice 8
lands on `topo` portals rather than tiles if step 3 is far enough along
(Decision 4). Pours (`planes.plane_pours`) are computed from copper
after routing and accept arcs already; vector copper changes nothing there.

**Bundling.** Reto's rule: a bundle of about 5 wires is fine, crosstalk
acceptable, cost-function aided. In `vroute`, the bundle discount prices a
segment that runs parallel to a bundle-compatible neighbour at
`w + warn_mm` (the stackup slice 7 predicate, class flag
`pcb_net_classes.rules.bundle`). In `topo`, a bundle is a ribbon: shared
portal sequence, packed at minimum pitch by the embedding; the discount is a
per-portal cost reduction in the LP objective, capped at a bundle width of
5 unless the class says otherwise.

**`pcb-global-plan-router.md`.** Its levels [A]/[B]/[C] stay; this item
replaces [B]'s tile graph with the CDT dual (slices 5–6b) and replaces [C]'s
maze with `vroute` (slices 3–4, retired in slice 10). Its slices 2 (pin assignment inside the plan, min-cut witness)
and 4 (loop) carry over to `topo`. `globalplan.py` stays as the probe and
fallback until slice 6a ties or beats it, and is retired in slice 10.

**`pcb-missing-constraint-classes.md` §E-1.** Slice 1 + slice 3 close the
"router half": pairwise voltage clearance reaches copper through `required()`.

**e8753d909 and gr489923.** e8753d909 stays reverted and is not relanded.
Its test `tests/test_pcb_maze_escape_stub.py` (deleted in 4949f7ee6;
recover with `git show e8753d909:tests/test_pcb_maze_escape_stub.py`) is
the seed fixture for slice 3. Slice 3 closes gr489923 and gr491588 with its
measured table; the thread file `docs/backlog/threads/pcb-easyeda-round-trip.md`
item 1 updates in the same commit.

## Slices

Each lands alone. Gate on every slice that touches routing: the **nano
floors** (`tests/test_pcb_nano_usb_pd_testboard.py`, seed 1: routed ≥ 35,
failed ≤ 29, `silk_missing` ≤ 36, `clearance` warns ≤ 9, no other rule
rises), the **taz15-blank** count (dev replay: `scripts/pcb-snapshot
export-prod taz15-blank`, source poses, `fixed='both'`, GND→B.Cu and
3V3→F.Cu declared, `iters=1 seed=1`), the five esp32c3 fab-render seeds
(`KNOWN_OPEN_DRC_ERRORS` unchanged), the ewod replay (≥ 51/55), the QFP
plaza member (≥ 53/55), and wall time per board against slice 0's table.
Wall-time figures below are targets, not gates, pending open question 8
(proposal: quality first, the job wall as the only hard cap). The maze is
the first pass in slices 3–9 only; slice 10 removes it. No slice carries a
migration; any that grows one ships via `/go`.

### Slice 0 — measurement harness

A script (`scripts/pcb-route-bench`) and a marked-slow test that route the
benchmark set and write one table: routed/failed, DRC errors and warns by
rule, vias, wire length, wall time, seconds per net and per connection,
per board. It also records whether `pcb_route` runs under a hard wall
(none found in `pcb_route.py`; the runner's `deadline_s` bound is the
mechanism if one is added). taz15-blank runs only where
the gitignored snapshot exists (the `test_taz15_import_export_reimport_structural`
pattern). No behaviour change.

Exit: the table for current main is written into this item; reruns are
deterministic (same counts twice).

### Slice 1 — clearance rules by (class pair, layer)

`rules.py`: `clearance_by_layer`, `pair_rules`, per-layer `working_voltage_v`
derivation (outer/inner, coated where the coat covers), and
`required(a, b, layer, ctx)`. `drc.check_clearance` reads `required()`;
`maze.OccupancyGrid.extra_mm` becomes per (net, layer). Validation in
`handlers/pcb.py` refuses unknown layers and classes in the new keys.

Exit: the EWOD HV array (`ARR1`, working_voltage_v=250, B.Cu 0.4 mm, F.Cu
class floor) puts without the `track[ARR1_R0C2] ↔ pad[ARR1/R1C2]` refusal,
and B.Cu pairs under 0.4 mm still ERROR (new tests in `tests/test_pcb_rules.py`,
`tests/test_pcb_drc.py`); a board with none of the new keys is bit-for-bit
unchanged (nano, esp32c3 DRC identical).

Owning files: `src/precis/pcb/rules.py`, `drc.py`, `maze.py`, `realize.py`
(`_net_clearances`), `src/precis/handlers/pcb.py`, skill
`precis-pcb-route-help`.

### Slice 2 — the clearance kernel

`clearance.py` primitives, analytic distances, per-layer index,
conservative polygonisation. `drc.check_clearance`, `check_via_via_keepout`,
`check_via_pad_keepout`, `check_npth_clearance`, `check_board_edge_clearance`
re-based on it. `clearance_violations_naive` extended to arcs and rectangular
pads and asserted equal to the kernel over randomized layouts.

Exit: every fixture board's DRC findings identical by `finding_key`, except
arc-track findings, whose margins change by at most the old chord error
(listed in the commit); oracle agreement on ≥ 500 randomized layouts with
arcs and rect pads; `view='drc'` wall time within +20 % of slice 0.

Owning files: `src/precis/pcb/clearance.py`, `drc.py`, `tests/test_pcb_clearance.py`,
`tests/test_pcb_drc.py`.

### Slice 3 — vector escape for walled-in segments

`vroute.py` (C-space, lazy tangent graph, octilinear conversion, via sites)
and the post-pass in `realize._realize_maze` for `no_path`/walled-in
failures only. Realizer helpers that answer legality (`_drop_via_site`,
`_via_clears_vias`, `_clears_foreign_pours`) move onto the kernel in the same
slice so the post-pass and the stitcher agree.

Exit: taz15-blank ≥ 149/151 (CN4_40/42/56/58/60/67/74, MCU_BOOT realized),
0 nets stripped by post-route DRC; nano floors hold; esp32c3 seeds and
ewod replay not worse; post-pass wall time reported per board (target
≤ 10 % of the maze's). gr489923 and gr491588 closed with the table.

Carried from gr491588 / gr489923 (stub measurements; Reto 2026-10-16 ruling:
the last-pass stub idea "will not be built", gr491588 stays open for this
slice):

- **Trigger.** The walled-in test is not specific to taz15. In
  router-lane-fix's `804d4d7dc` experiment (branch only) nano's USB-C
  0.5 mm-pitch pads also had no passable grid cell, so a stub gated on
  "walled in" still fired there (nano seed 1: 34/30, 37, 9, against the
  floors 35/29, 36, 9). The post-pass therefore fires on nano; slice 3
  reports the fire count per board (nano, taz15-blank, esp32c3 seeds, ewod).
- **Order.** The post-pass runs after the maze has placed everything, so it
  cannot move earlier nets. What it can still move is silk, labels and pours
  placed after routing (Risks table; 63 firings in the stub run moved two
  clearance warns nowhere near a stub). Exit must show `silk_missing` and
  `clearance` warns at or under the floors, not only routed/failed.
- **Noise.** Nano swings about ±2 nets on small routing changes (gr491588).
  Reto decides: is the floor judged on seed 1 alone, or on a stated seed set
  with the worst seed as the gate.
- **Fixture.** The source of the failing pad geometry is gr489923: CN4
  `CONN-SMD_APCI0136-P001A`, 69 pads 0.3 × 1.55 mm in two staggered rows at
  0.5 mm pitch, pad gap 0.2 mm; a 0.3 mm track along the pad axis clears a
  neighbour by 0.2 mm > 0.152 mm, so a legal exit exists. Repro:
  `scripts/pcb-snapshot export-prod taz15-blank`; load;
  `UPDATE pcb_instances SET fixed='both'`; `pcb_route._dispatch iters=1`.
  Backup branch `rlf-full-backup` holds the stub experiment.

Owning files: `src/precis/pcb/vroute.py`, `realize.py`, `tests/test_pcb_vroute.py`.

### Slice 4 — vector fallback for every maze failure

The post-pass widens to all failed segments, most-constrained first, with the
shared route cost (`routecost.py`, slice 4 creates it; `maze._step_penalty`
reads it, bit-for-bit). A segment that fails in `vroute` keeps the maze's
`UnroutedReason`, plus a `vroute` reason kind.

Exit: on every benchmark, routed ≥ slice 3 and floors hold; nano routed
count reported (expected to rise; `silk_missing` and `clearance` must not).
Decision recorded: does `vroute` run first on fine-pitch pads (pads whose
slack is below one grid cell)? Only if it measures better.

Owning files: `vroute.py`, `realize.py`, `maze.py`, `src/precis/pcb/routecost.py`.

### Slice 5 — CDT region graph and portal capacities (probe)

`topo.build_region_graph` per layer from the kernel's inflated obstacles,
physical portal capacities, `view='congestion'` gains a portal map. Measure
against `globalplan.build_tile_graph(capacity="flow")` on the ewod replay
and the QFP plaza member: does every net's escape exist as a portal
sequence where the tile model reads 0 lanes?

Exit: the comparison table in this item; no default changes.

Owning files: `src/precis/pcb/topo.py`, `tests/test_pcb_topo.py`.

### Slice 6a — PathFinder on the CDT (baseline)

Port `globalplan.plan_routes`' negotiated congestion (present + history
cost, pin-swap sinks via `pinswap._hungarian`) from tiles to the CDT dual's
portals and via faces (Decision 11). Its converged paths seed slice 6b's
homotopy candidates. Opt-in route param `router='topo'` beside `corridor=`.

Exit: topology-feasible counts (four counts, step 3) on the benchmark set,
against the tile plan (ewod replay strict: 35/55 planned); plan time
reported on taz15-blank.

Owning files: `topo.py`, `realize.py`, `src/precis/workers/job_types/pcb_route.py`
(param), `PARAMS_SCHEMA` (gr469872).

### Slice 6b — topology assignment (LP/ILP)

Homotopy candidates per branch (6a's paths plus k-shortest sleeves),
path-based MCF in HiGHS, lazy cuts,
rounding and residual ILP, pin-swap sinks, ribbon order at congested
triangles, min-cut witness into `ConflictDigest`. Ribbons persisted in
`pcb_routes.meta.ribbon`. Selected under `router='topo'` with
`topo_solver='lp'|'pathfinder'`.

Exit: the LP ties or beats 6a's topology-feasible count on every benchmark;
on the ewod replay the plan is capacity-feasible for ≥ 53/55 under strict
participant rules; LP + ILP time reported on taz15-blank (target ≤ 30 s).
If the LP does not beat 6a, PathFinder stays the solver and the LP is
dropped.

Owning files: `topo.py`, `tests/test_pcb_topo_lp.py`.

### Slice 7 — geometric embedding and SCP cleanup

Funnel string-pulling, tangent arcs, order-preserving offsets, 45°
conversion, SCP spacing cleanup (HiGHS QP, active set from the kernel),
kernel verification, no-good cuts back to slice 6b (or 6a's history cost).
Arcs only where a round obstacle forces the hug (Decision 3).

Exit: `router='topo'` ties or beats maze + `vroute` on routed nets on every
benchmark with floors holding; wall time reported against slice 4's (target
≤ 2×). When it ties or beats on all five, `router='topo'` becomes the
default and the maze runs only as a comparison in slice 0's harness.

Owning files: `topo.py`, `vroute.py` (shared tangent geometry),
`tests/test_pcb_topo_embed.py`.

### Slice 8 — bundling and stackup costs at topology level

Bundle ribbons (cap 5 per class default), per-layer cost, ground-severance
per portal (stackup slice 8's cost, on portals), `reference` soft cost.

Exit: the synthetic 5-wire fixture (`qfp_fanout.py` family) routes as one
ribbon on the fill layer; pour fragments on the four stackup slice-1 boards
do not rise; routed does not fall.

Owning files: `topo.py`, `routecost.py`, `planes.py` (severance query).

### Slice G — GPU benchmark (only after slice 7)

Time the SCP cleanup's distance + Jacobian evaluation on CPU (numpy/numba)
and on a GB10 (JAX, Decision 10) on taz15-blank and a 4×
synthetic board, as a job on the compute node with CPU fallback. Decision
rule: GPU work proceeds only if the end-to-end route job is ≥ 3× faster on
a board we route.

### Slice 9 — step 4 (optional)

Teardrops, length matching; specced after slice 7.

### Slice 10 — retire the maze (Decision 1)

Delete grid routing: `maze.OccupancyGrid.route`/`_route_in`/`_astar_kernel`,
`Negotiation`, the reorder passes in `realize._realize_maze` (becomes
`_realize_vector`), `globalplan`'s tile graph and corridor hand-off, the
`corridor=`/`escape='plaza'`/`negotiate=` route params (refused with a
pointer to `router='topo'` for one release, then removed from
`PARAMS_SCHEMA`). Grid-only consumers move to the kernel first: the
stitcher's and `_plane_fanout`'s site checks (done in slice 3), stackup
slice 1's drop-via pre-claim (a kernel reservation), `_courtyard_body_mask`
(an under-body polygon cost). `view='congestion'` shows portals only.

Exit, all on vector-only routing (no `OccupancyGrid` on the route path)
against maze-first measured in the same harness run:
- nano seed 1: routed ≥ 35, failed ≤ 29, `silk_missing` ≤ 36, `clearance`
  warns ≤ 9, no rule rises;
- taz15-blank: realized ≥ the maze-first count (≥ 149/151 since slice 3);
- esp32c3, five fab-render seeds: routed ≥ maze-first, `KNOWN_OPEN_DRC_ERRORS`
  unchanged;
- ewod replay ≥ 51/55, QFP plaza member ≥ 53/55;
- within the runtime budget open question 8 settles.
Any miss leaves the maze in place and lists the boards and nets that miss.

Owning files: `maze.py` (shrinks to whatever grid helpers survive, or is
deleted), `realize.py`, `globalplan.py`, `pcb_route.py`, `handlers/pcb.py`,
skill `precis-pcb-route-help`, `docs/codebase.md`, the tests that pin
maze behaviour (rewritten against `vroute`/`topo` or deleted).

### Slice 11 — per-PCB any-angle option (later, Decision 2)

A board-level flag (`pcb_boards.meta.trace_angle='any'`, default `'45'`)
that skips the octilinear conversion and exempts the board from
`check_octilinear`. Specced after slice 10.

## Risks

| Risk | Mitigation |
|---|---|
| Polygonisation error opens a false corridor | Verdict and tangent graph are analytic; polygons only for CDT and pours, always outward; δ = 5 µm (Decision 6) |
| Floating-point degeneracies (collinear tangents, near-tangent arcs) | Snap to the Gerber unit (1 nm, `geom.GERBER_UNIT_MM`) after construction; treat tangency within δ as contact |
| Order dependence (sequential post-pass) | Post-pass never moves earlier copper; slices 6a/6b remove it for topology |
| Via-site sampling misses the only legal site | Sites include pad-end escapes and `_drop_via_site` candidates; slice 3 logs per failure how many sites were tried; lattice pitch is a parameter |
| Tangent graph blow-up (Θ(n²) bitangents) | Lazy generation from expanded nodes, STRtree-pruned; window around the segment as the maze's `search_window` |
| 45° conversion loses a free-angle corridor | Conversion is part of edge legality, not a post-snap; a dropped edge is visible in the failure witness |
| Topology feasible, geometry not | Four separate counts; no-good cuts; kernel verification before commit |
| Silk and pours shift downstream of new copper (gr491588's mechanism) | Kernel treats silk boxes and courtyards as obstacles at their rule distance; the floor gate catches the rest |
| Runtime | Slice 0 table per board size; targets per slice; job wall as hard cap (open question 8) |
| Kernel and DRC become one code path, so a kernel bug passes DRC | The naive oracle stays independent and covers arcs and pads (slice 2) |

## Related

- [pcb-stackup-regions](pcb-stackup-regions.md) — shared route cost, slice ordering.
- [pcb-global-plan-router](pcb-global-plan-router.md) — levels [A]/[B]/[C]; [B] moves to the CDT.
- [pcb-global-plan-probe](pcb-global-plan-probe.md) — tile-model evidence.
- [pcb-missing-constraint-classes](pcb-missing-constraint-classes.md) §E-1 — the router half of pairwise clearance.
- [pcb-lazy-netlist-and-checks](pcb-lazy-netlist-and-checks.md) — one check surface; kernel findings keep that surface.
- gr489923, gr491588 (CN4 escape, stub regression), gr489914 (same-net vias).

## References

- Dai, Dayan, Staepelaere, "Topological routing in SURF", DAC 1991, doi 10.1145/127601.127622 (pa266319).
- Dai, Kong, Sato, "Routability of a rubber-band sketch", DAC 1991, doi 10.1145/127601.127623 (pa266321).
- Staepelaere et al., "SURF: rubber-band routing system for MCMs", IEEE D&T 1993, doi 10.1109/54.245960.
- Liu et al., "Substrate topological routing for high-density packages", TCAD 2009, doi 10.1109/TCAD.2008.2009154 — PCDT capacities, 349–990 nets in 1.5–27 s.
- Liu & Arimoto, tangent graph among curved obstacles, IJRR 1992, doi 10.1177/027836499201100409.
- Lozano-Pérez & Wesley, configuration space, CACM 1979, doi 10.1145/359156.359164.
- Wein, exact and conservative offset polygons, CAD 2007, doi 10.1016/j.cad.2007.01.010.
- Lin et al., unified PCB routing, ASP-DAC 2021, doi 10.1145/3394885.3431568 — 0.05 mm grid A*, 100 % on 14 boards, 3–25 247 s.
- Schulman et al., TrajOpt, IJRR 2014, doi 10.1177/0278364914528132.
- Ratliff et al., CHOMP, ICRA 2009, doi 10.1109/ROBOT.2009.5152817.
- Zhang et al., Cypress GPU PCB placement, ISPD 2025, doi 10.1145/3698364.3705346.
- Lin et al., GAMER GPU maze routing, TCAD 2022, doi 10.1109/TCAD.2022.3184281; Han et al., FastGR, doi 10.1109/TCAD.2022.3217668.
- Lu & Yang, cuPDLP.jl, doi 10.1287/opre.2024.1069.
- Open source to read before building: Topola (Rust, MIT, topological router), gEDA `toporouter.c` (Dayan-based), Freerouting rooms/doors (`docs/architecture.md`).

All DOIs come through Perplexity (`491905`, `491939`, `491941`) and are
unverified; import with `put(kind='paper')` before citing in a draft.
