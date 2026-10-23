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
| ewod replay | The tile plan (deleted `globalplan.py`) could not express the braid an escape needs inside one tile; every plan-guided variant tied or lost to plain 51/55. | `pcb-global-plan-router.md` slices 0–1b (deleted; git history) |
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
  `pcb-global-plan-router.md` slice 4 — deleted with the grid router; git history).

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

## Decisions (Reto 2026-10-18)

Reto, ~16:0xZ in the coordinator window, verbatim: "So, push the vector stuff, and leave this alone for now. I am even thinkin of dropping the limits for the grid router now, because it just causes issues. vector first and best algorithm. Also, shove the parts and the vias as needed, maybe even gradual weights that shift stuff and multi iter place route. vector first." And just before that: "The thought is that these boards should be easy to route with the proper algo. No ratchet needed, that seems like incremental and painfult."

| # | Question | Ruling |
|---|---|---|
| 13 | Priority | **Vector first, best algorithm.** This item is the pcb routing critical path. Work that only tunes the maze (stackup default change, placer silk spacing, THT pad model, plane stitching) is held, and nothing new targets maze floors. |
| 14 | Target | **Absolute, not ratcheted.** The goal on the benchmark boards is every net routed with 0 DRC errors. Maze per-seed floors are dropped now (Reto 2026-10-19); the target stays absolute: every net routed, 0 DRC. The benches keep printing their numbers. |
| 15 | Shove | **Batch shove of parts and vias as needed.** The router may move vias, and nudge parts, to open a route, using gradual weights that shift things rather than hard keep-outs. This is batch, not interactive; Non-goals keeps "interactive push-and-shove". |
| 16 | Place-route loop | **Multiple place-route iterations.** Placement and routing alternate, with placement costs fed by routing congestion and failures, rather than a single place-then-route pass. Builder proposes the slice shape (proposal 13–16 below), coordinated with `pcb-guided-place-route.md` and `pcb-global-codesign-north-star.md`. |
| 17 | Via spans | **Every via is a through via** (Reto 2026-10-18, gr495289: "Buried (partial) vias cost a lot extra, and we don't want to pay"). A via reserves its disk on every copper layer and the pour pass cuts an antipad on each layer where it does not connect. The maze router (`realize.through_span`) and the vector router both emit through spans only. |

**Proposal (needs a builder's design, then Reto):** how 15–16 slot in. Via shove belongs with slice 3/4 (vector escape: a via that blocks a walled-in segment may move within its net's legal region). Part nudge plus the place-route loop is a new slice after slice 5's region-graph probe, because congestion per region is the signal the placer consumes.
Placement, ruled 2026-10-18: §Decisions 15–16 in the vector slices
(after slice 5); the cost, shove and loop design is
[pcb-unified-place-route](pcb-unified-place-route.md).

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
sagitta (5 segments per quadrant at R = 0.3 mm, 12 at R = 2 mm).

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

The tile graph's measured limit is the intra-tile braid (`pcb-global-plan-router.md`, deleted — git history
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
   expansion moves and the LLM menu (`pcb-global-plan-router.md`, deleted — git history).

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

**`pcb-global-plan-router.md` (deleted with the grid router — git history).** Its levels [A]/[B]/[C] stay; this item
replaces [B]'s tile graph with the CDT dual (slices 5–6b) and replaces [C]'s
maze with `vroute` (slices 3–4, retired in slice 10). Its slices 2 (pin assignment inside the plan, min-cut witness)
and 4 (loop) carry over to `topo`. `globalplan.py` stays as the probe and
fallback until slice 6a ties or beats it, and is retired in slice 10.

**`pcb-missing-constraint-classes.md` §E-1.** Slice 1 + slice 3 close the
"router half": pairwise voltage clearance reaches copper through `required()`.

**`precis.pcb.routeclean` (gr495397).** A shipped maze-route post-pass
that moves another net's blocking via so a kinked track runs straight.
Trigger: a routed segment with more bends than its shortest 45° path needs
(0 when the pins line up on a multiple of 45°, else 1), or longer than
1.15× that path by at least 0.1 mm. This replaced the first spec's ">2
bends", which missed a one-via jog (2 bends at 1.06×). It replays the
maze's recorded paths, so slice 10 (retire the maze) has to carry it over
or drop it.

**e8753d909 and gr489923.** e8753d909 stays reverted and is not relanded.
Its test `tests/test_pcb_maze_escape_stub.py` (deleted in 4949f7ee6;
recover with `git show e8753d909:tests/test_pcb_maze_escape_stub.py`) is
the seed fixture for slice 3. Slice 3 closes gr489923 and gr491588 with its
measured table; the thread file `docs/backlog/threads/pcb-easyeda-round-trip.md`
item 1 updates in the same commit.

## Slices

Each lands alone. Gate on every slice that touches routing: the **nano
floors** (`tests/test_pcb_nano_usb_pd_testboard.py`, seed 1: routed ≥ 39,
failed ≤ 25, `silk_missing` ≤ 32, `clearance` warns ≤ 10, no other rule
rises; mean over seeds 1–8 ≥ 36.0, every seed ≥ 33; raised at slice 3a),
the **taz15-blank** count (dev replay: `scripts/pcb-snapshot
export-prod taz15-blank`, source poses, `fixed='both'`, GND→B.Cu and
3V3→F.Cu declared, `iters=1 seed=1`), the five esp32c3 fab-render seeds
(`KNOWN_OPEN_DRC_ERRORS` unchanged), the ewod replay (≥ 51/55), the QFP
plaza member (≥ 53/55), and wall time per board against slice 0's table.
Wall-time figures below are targets, not gates, pending open question 8
(proposal: quality first, the job wall as the only hard cap). The maze is
the first pass in slices 3–9 only; slice 10 removes it. No slice carries a
migration; any that grows one ships via `/go`.

### Baseline (slice 0, shipped)

`scripts/pcb-route-bench [--twice] [OUT.md]` routes the set
(`tests/fixtures/pcb/route_bench.py` says how each board is built) and
writes this table plus a JSON of every count and
`rule:severity:finding_key`; `--twice` fails unless both runs match.
taz15-blank joins only when its gitignored export exists
(`.scratch/pcb-snapshot/taz15-blank.json`). Hard wall: none —
`pcb_route` has no deadline or timeout; only realize's
`negotiate_budget_s` bounds the negotiation loop.

Measured 2026-10-18 on main 7c03ac7dc, two runs, every count and finding
key identical (wall and DRC seconds from the first run; the rerun's were
within 2 s and 1.4 s). `routed/nets` counts nets with two or more members,
so taz15-blank reads 64/79; the 139/151 and 149/151 above count every net.
"DRC s" is the recorded `view='drc'` run on the job boards and
`session.routed_drc_findings` (copper rules only) on ewod-replay and
qfp-plaza.

| board | routed/nets | failed | conns | vias | wire mm | wall s | s/net | s/conn | DRC s | DRC errors | DRC warns |
|---|---|---|---|---|---|---|---|---|---|---|---|
| nano | 35/64 | 29 | 206 | 92 | 2082.3 | 71.5 | 1.12 | 0.347 | 9.00 | 75: connectivity 11, silk_missing 35, unrouted 29 | 12: board_edge_clearance 2, clearance 9, silk_edge_clearance 1 |
| esp32c3-s1 | 11/11 | 0 | 61 | 29 | 214.2 | 10.4 | 0.95 | 0.170 | 1.60 | 0 | 87: clearance 63, trace_width 24 |
| esp32c3-s2 | 11/11 | 0 | 61 | 31 | 242.4 | 11.2 | 1.02 | 0.184 | 1.76 | 0 | 78: clearance 54, trace_width 24 |
| esp32c3-s3 | 11/11 | 0 | 61 | 33 | 206.5 | 13.8 | 1.25 | 0.226 | 1.92 | 0 | 83: clearance 57, trace_width 26 |
| esp32c3-s4 | 11/11 | 0 | 61 | 32 | 225.4 | 11.3 | 1.03 | 0.185 | 1.74 | 0 | 79: clearance 55, trace_width 24 |
| esp32c3-s5 | 11/11 | 0 | 61 | 32 | 217.6 | 10.5 | 0.95 | 0.172 | 1.72 | 0 | 81: clearance 55, silk_edge_clearance 2, trace_width 24 |
| ewod-replay | 51/55 | 4 | 55 | 25 | 454.9 | 32.9 | 0.60 | 0.598 | 0.43 | 0 | 160: annular_ring 55, clearance 50, trace_width 55 |
| qfp-plaza | 53/55 | 2 | 55 | 35 | 271.3 | 4.8 | 0.09 | 0.087 | 0.05 | 39: clearance 39 | 178: annular_ring 90, trace_width 88 |
| taz15-blank | 64/79 | 15 | 236 | 143 | 1682.5 | 47.1 | 0.60 | 0.200 | 33.39 | 23: connectivity 4, silk_missing 4, unrouted 15 | 366: board_edge_clearance 2, clearance 333, courtyard_overlap 30, unconnected_pin 1 |

Failed nets: ewod-replay ARR1_R1C5, R6C5, R7C2, R7C6; qfp-plaza N27, N49;
taz15-blank C9_2, CN4_40/42/56/58/60/67/74, GND, MCU_BOOT, MCU_RST, PV+,
USB2_A5, USB2_B5, USBD-; nano the 29 of its floor test.

### Slice 1 — clearance rules by (class pair, layer) (shipped)

Shipped: `clearance_by_layer` and `pair_rules` on `pcb_net_classes.rules`,
`rules.required(a, b, layer, ctx)` read by `drc.check_clearance`,
`OccupancyGrid.extra_mm(net, layer)`, and the P7 refusals in
`handlers/pcb.py` (`op='class_rules'` and the design put). One defect
found on the way: `clearance_pairs_indexed` reported a through-via pair on
its first layer only, so a floor set on B.Cu alone would never be read for
it; with layer-scoped rules a multi-layer pair is now judged on its
strictest layer (boards without them keep the old path). The EWOD exit
holds (the two EWOD HV tests in `tests/test_pcb_drc.py`, e.g.
`tests/test_pcb_drc.py::test_ewod_hv_per_layer_floor_admits_the_f_cu_stubs_and_keeps_b_cu_at_0_4`,
and `tests/test_pcb_clearance_rules_put.py`).

Bench at slice 1 against the slice 0 baseline: every board's counts and
every `rule:severity:finding_key` identical (none of the benchmark
boards carries the new keys); wall and DRC seconds within run-to-run noise
(DRC s: nano 9.00 → 7.23, esp32c3 1.60–1.92 → 1.63–1.95, ewod-replay
0.43 → 0.50, qfp-plaza 0.05 → 0.05, taz15-blank 33.39 → 33.23).

**Slice 1 decisions (builder V proposal; coordinator ruling 2026-10-18:
P1–P3, P6, P7 approved as written; P4/P5 reduced as stated).**

- **P1. Layer entries.** A key in `clearance_by_layer[L]`
  (`clearance_mm`, `min_clearance_mm`) replaces the class value of that
  tier on L, and the `working_voltage_v` floor does not apply on L for a
  tier the entry names. The fab minimum still clamps both tiers.
- **P2. Pair entries.** `{"with": <class>, "layers": [...],
  "min_clearance_mm"?, "clearance_mm"?}`; `layers` omitted means every
  copper layer. A rule on A "with B" also covers (B, A); when both classes
  name each other on L, the larger value wins. A matched entry replaces
  the pair's per-net value (the larger of the two nets) for each tier it
  names; a tier it omits keeps the per-net value; the warn tier is never
  below the error tier.
- **P3. Pairwise voltage** (`net_voltages`, `|V_a − V_b|`) is unchanged:
  it raises the warn tier by `max()` after the pair rule.
- **P4. Coating column.** Slice 1 keeps today's B4 floor for a class's
  `working_voltage_v` on every layer, so the slice is behaviour-neutral
  and no clearance falls. The per-layer column (B1 inner, B2/B4 outer by
  a `coated` flag) is a slice 2 proposal below; lowering inner layers
  needs Reto's ruling.
- **P5. Router.** The maze reserves each net's own per-layer warn tier
  (`OccupancyGrid.extra_mm(net, layer)`); pair rules stay DRC-only, as the
  caller table says.
- **P6. Scope of `required()` in slice 1:** same net (`none`), class pair,
  voltage, per-net per-layer floors. The same-footprint coated WARN stays
  in `check_clearance`; the via, edge and NPTH kind rules move in slice 2
  with the kernel.
- **P7. Validation.** `clearance_by_layer` keys and `pair_rules[].layers`
  must name copper layers of the board's stackup (routable or not);
  `pair_rules[].with` must name a class on the design or in the same put;
  an entry key outside the ones above is refused.

Follow-on once this lands: put EWOD ruling 19 (`working_voltage_v: 250`
on the `ewod_{name}_escape` class) into the generator
(`docs/backlog/ewod-controller-and-hv-supply.md`; Reto, 2026-10-16 ~16:05Z, via the fleet coordinator: "add the constraint when it is supported.")

### Slice 2 — the clearance kernel (shipped)

Shipped: `clearance.py` (bodies = exact core + arcs + radius, closed-form
arc distances, a box STRtree per layer, `outward_circle`);
`clearance_pairs_indexed`, `check_via_pad_keepout`, `check_npth_clearance`
and `check_board_edge_clearance` measure through it; the same-net via
hole-wall rule moved into `rules.required()`; `clearance_violations_naive`
measures arcs and rect/obround/polygon pads and agrees with the kernel on
500 random layouts (`tests/test_pcb_clearance.py`). A pour against an arc
track culls the pour's edges exactly (numpy box test under an upper bound
from the arc's start point) before the closed form.

Bench against slice 1: routing identical on every board. New `clearance`
warns, all arc track against a pour, each gap moved by at most the old 15°
chord error, R·(1 − cos 7.5°) (1–2 µm at the fillet radii here): esp32c3
seeds 1–5 +5/+7/+6/+7/+8 (seed 3 also loses one), taz15-blank +48; nano,
ewod-replay and qfp-plaza identical. Pad and via gaps moved by ≤ 0.05 µm
(the old 64-segment circles), flipping no finding. `view='drc'` seconds:
nano 7.50 → 7.58, esp32c3 1.64–2.00 → 1.39–1.66, ewod-replay 0.50 → 0.46,
qfp-plaza 0.05 → 0.05, taz15-blank 34.16 → 30.36. Those pours were cut
against the same chorded tracks (`planes.py` blockers from
`drc._copper_item_polygon`); cutting them with outward kernel polygons is
the next commit, so pours are generated legal instead of flagged.

**IPC column per layer (from slice 1 P4; built in slice 2).** Reto,
2026-10-18, in the builder's session: "Yes, IPC column per layer". The
rule context carries `coated` (`gerber.declares_parylene_coat`), and a
class's `working_voltage_v` floor reads the IPC-2221B column per layer:
B1 on inner layers, B4 on outer layers of a coated board, B2 on outer
layers of an uncoated board. Effects: inner-layer floors fall (250 V:
0.4 → 0.2 mm); outer floors on an uncoated board rise (250 V: 0.4 →
1.25 mm). The router's per-(net, layer) reservation reads the same column.
Known limit: the router decides "coated" from electrode pads only
(mask-open-only arrays are not in its input), so on such a board it
reserves the B2 figure where DRC asks B4 — more room than needed, never a
DRC error. No benchmark board carries a class `working_voltage_v`; EWOD
boards (coated, the only generator that emits one) change on inner layers
only.

### Slice 3a — vector escape post-pass (shipped)

Shipped: `vroute.py` and `vroute.escape_pass`, run in
`realize._realize_maze` after the clean-up pass and before pours
(`RealizeConfig.vector_escape`, default on; a budget of 60 000 A*
expansions per board, at most 6 000 per connection: work, never the
clock, so a board routes the same on every machine). Every
failed non-plane segment is routed gridless against the frozen copper:
octilinear A* over `(node, layer)`, nodes = the octagon corners circumscribing
each inflated obstacle (exact support values, a track split per segment and
arc) plus escape rays from both terminals; every leg is checked by the slice
2 kernel. Vias are through-only (gr495289 c9) and keep the clearance from
every pad and via of any net (no via-in-pad, gr496850). A net commits only
if all its failed segments route and a re-pour of the board leaves no plane
segment unserved and no extra pour fragment; otherwise it is rolled back. A
fillet whose inward bulge fails the kernel falls back to straight legs:
`_track_from_run`'s subset argument bounds the centreline's deviation, not
the copper's, so a fillet can sit up to a full width past the straight
track. Summary line `vector escape: K of N failed segments routed gridless`.

Bench (gate: never worse on any board, Reto 2026-10-18): main 9f914da24,
then this slice on it, `--twice` (both runs identical):

| board | routed | failed | DRC errors | wall s | DRC s |
|---|---|---|---|---|---|
| nano | 34 → 39 | 30 → 25 | 73 → 66 | 65 → 82 | 15.5 → 16.1 |
| taz15-blank | 65 → 73 | 14 → 6 | 20 → 11 | 34.5 → 51 | 33.1 → 33.9 |
| qfp-plaza | 53 → 54 | 2 → 1 | 39 → 39 | 4.8 → 5.3 | 0.05 |
| esp32c3 s1–5, ewod-replay | identical | | | | |

Nano over seeds 1–8: main 34/33/43/34/31/33/31/37 (mean 34.5), this
slice 39/35/46/35/33/35/35/38 (mean 37.0). Nano's warns hold (clearance
10, `board_edge_clearance` 2). qfp's warns 178 → 181 are the newly
routed net's own annular-ring and trace-width warns. The wall-time rise
is the escape pass itself (taz15 +17 s against a 34.5 s maze); slice 3b
owns the time.

### Slice 3b — reach the rest, and the time

Part-landed 2026-10-22 (router-a, gr503666): the maze is gone
(d42369679), so `vroute` is the whole router, not a post-pass. This
landing adds (1) a per-segment failure reason: the search records
witnesses (`vroute` `fail_stats`: start/goal leg counts, via sites and
hops, layers reached, sampled blocker obstacles) and the diagnosis maps
them onto the `UnroutedReason` kinds — the new `'keepout'` (only
non-copper refused the terminal legs: a reserved region, a silk
keep-out, a fiducial/NPTH ring) and `'via_blocked'` (the goal's layer
was never reached: the layer transition, not a corridor, is missing)
beside `'no_path'`/`'width'`/`'congestion'`/`'search_budget'`; the job
summary prints one line per unrouted segment and `last_route` carries
`unrouted_reasons`; (2) a declared-plane-layer cost
(`RealizeConfig.plane_layer_cost`, 2.5× — inside the stackup spec's
"about 2–3×" ruling) on `vroute.Request.layer_cost`, so signals prefer
a free inner layer over cutting the pour; (3) one bounded retry round
for failed routable nets on the full board window from a dedicated
pool (`RealizeConfig.retry_expansions`).

Measured 2026-10-22 on `tests/fixtures/pcb/route_bench.py::bench_esp32c3`
(seeds 3–5, the gr503666 boards): routed 10/11, 10/11, 9/11 → 11/11 on
all three (target: all 14 segments — met); B.Cu signal track falls to
0 mm on every seed (was the dominant signal layer) and In1+In2 rise from
~28–68 mm to ~176–203 mm — the pour stays whole. Witness data on the
failures: searches ended `exhausted` with every leg into the goal
refused by other nets' copper on F.Cu — congestion at the pad entry,
which the surcharge relieves by keeping the outer layers sparse.

Re-measured 2026-10-22 after the field placer landed (29ed73add rotates
U1 by 45° on some seeds): s3 10/11 (SDA), s4 11/11, s5 10/11 (SDA).
Root cause (gf-pcb-route-1): the plane fan-out runs before any signal
routes and `_drop_via_site` takes the first legal site around its pin,
so the 3V3 drop via landed 1.04 mm from U1.SDA on SDA's only
perpendicular exit out of the 0.65 mm row; TXD/RXD then closed the rest
and SDA `exhausted` with every goal leg refused. Fix: `_escape_lanes`
— every point-to-point-routed SMD pad in a fine-pitch row gets a lane
(perpendicular to the row, away from the centroid, two via pitches
long) and the sweep prices a via or stub in a lane ahead of ink and
body (soft, least-bad fallback). Measured seeds 1–5: 10, 11, 10, 11, 11
(was 10, 11, 10, 11, 10); s5's SDA routes, s3's SDA routes but SCL now
fails. The remaining s1/s3 failures are the same shape one layer up:
U1.SCL's goal legs are refused by SDA/TXD/RXD tracks and the GND/3V3
drops — the shortest-first order routes SCL (the long header net) last,
and the earlier signals' exits cut across its lane. That is a
signal-vs-signal lane problem (an escape pass per dense part before the
trunks, or lane-aware leg costs in the A*), not a fan-out one.

Still-open scope (coordinator ruling 2026-10-18, adjusted to the
vector-only world):

1. **Node coverage.** taz15-blank still fails CN4_42, MCU_BOOT, MCU_RST,
   USB2_A5, USBD- (and the plane nets GND, PV+); their searches end with
   the graph exhausted, not the budget, so the octagon-corner node set
   misses the corridor.
2. **Time.** The vector pass's wall time per board is reported against
   slice 0's table; and the silk cost: relabel only near the new copper.
3. **Trigger.** (Was "expose the maze's walled-in result" — the maze is
   removed.) The witness stats now say directly why a search stopped;
   nano's fire count is still reported per board.

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
  Reto 2026-10-18: nano floor = mean over seeds, per-seed catastrophe floor
  28; the product keeps single-seed placement for now, best-of-N not yet.
  The floors are dropped (§Decisions row 14, 2026-10-19); the nano test
  still runs `NANO_SEEDS` (one test per seed) and prints the per-seed counts.
- **Fixture.** The source of the failing pad geometry is gr489923: CN4
  `CONN-SMD_APCI0136-P001A`, 69 pads 0.3 × 1.55 mm in two staggered rows at
  0.5 mm pitch, pad gap 0.2 mm; a 0.3 mm track along the pad axis clears a
  neighbour by 0.2 mm > 0.152 mm, so a legal exit exists. Repro:
  `scripts/pcb-snapshot export-prod taz15-blank`; load;
  `UPDATE pcb_instances SET fixed='both'`; `pcb_route._dispatch iters=1`.
  Backup branch `rlf-full-backup` holds the stub experiment.

From pcb-router-fails-at-real-board-size (closed; the grid router it
targeted was deleted in d42369679):

- **Why the grid lost (measured 2026-10-01, same board, seed 1).** The
  imported 140-part board routed 8/89 whether placement was frozen or not,
  at iters 200 or 2000; only a finer grid moved it (0.1 mm pitch: 62/89).
  `maze.grid_for`'s 400 cells/axis gave 0.5 mm cells on a 200 mm board, so
  fine-pitch pads plus clearance dilation walled every pin in. Placement and
  anneal budget changed nothing; the remainder at fine pitch was congestion.
  The 2026-10-02 offline baseline (pcb 460559, frozen, seed 1) was 55/89.
- **Workflow hazard, still open.** `op='route'` runs the joint place+route
  anneal, so "re-route my board" silently re-places every unlocked part
  (103 of 140 on the import). Either the route op gets a mode that holds
  placement, or the surface says plainly that routing moves parts.
- **Open:** whether `iters` should scale with net count rather than being a
  caller-supplied constant.
- **Coupling:** `optimize._FIXED_VIA_CLEARANCE_MM` is pinned equal to the
  realizer's clearance by
  `test_pcb_optimize.py::test_a_land_may_not_sit_on_an_authored_via_but_the_body_may_cover_one`;
  change both or neither.
- **Harness:** the env-gated real-board tests read `PRECIS_EPRO_FIXTURE`,
  the only one of our env vars on the `scripts/test` passthrough allowlist,
  so route configs are test parameters, not env vars.

Owning files: `src/precis/pcb/vroute.py`, `realize.py`, `tests/test_pcb_vroute.py`.

### Slice 4 — every failed segment gets a reason and a second chance

(Updated 2026-10-22 for the vector-only world: the maze this slice was
a fallback FOR no longer exists; `vroute` is the router. The slice's
real content — failure reasons and the shared route cost — landed in
the 3b work above: every unrouted segment carries an `UnroutedReason`
kind plus the search's own witnesses, and `Request.layer_cost` is the
first shared route-cost term. What remains is `routecost.py` proper —
direction penalties, bundle discount — read by `vroute`'s step cost.)

Owning files: `vroute.py`, `realize.py`, `src/precis/pcb/routecost.py`.

### Slice 5 — CDT region graph and portal capacities (probe)

**Probe shipped** (measurement only, nothing routes through it):
`topo.build_region_graph` triangulates one layer's free space with every
obstacle boundary constrained, and `topo.portal_capacity` prices each
portal at `⌊(L − s)/(w + s)⌋`. `view='congestion'` prints a per-layer
portal map under the tile map: triangles, portals, portals that take no
track, and the median capacity. `tests/test_pcb_topo.py` holds the unit
tests and the two slow comparisons below.

**Second landing** (the funnel domain, 2026-10-20): `board_obstacles`
moved onto the slice-2 kernel — each item's `Body` drawn outward by
`clearance.outward_polygon` (≤ δ), so arcs stop being chorded here too —
and `Portal`/`RegionGraph` learned the pairwise world:
`portal_capacity` takes per-end clearances (`⌊(L − s_a − s_b + s)/(w +
s)⌋`), `RegionGraph.capacity_for` prices each portal end by
`rules.required` (an own-net end owes 0). The **net domain**
(`net_region_graph`, one per (net, layer)) inflates every foreign
obstacle by `w/2 + required(net, o, L).warn + δ` and drops own-net
copper — the centreline C-space the unified spec's taut bands run in
(§Taut bands; composition row "5 CDT portals"). On it: `locate`
(barycentric point-in-triangle), `dual_path`/`sleeve_between` (Dijkstra
over centroids), `band_in_sleeve` (Mononen's funnel — the taut
polyline), `funnel_path` (insertion-search alternative), and
`side_swap_candidates` (one alternative sleeve per banned portal — the
SIDE_SWAP generator). Library only: nothing routes through it yet;
`d-vecdefault` owns the switch. Unit tests pin triangulation validity
(no triangle crosses an inflated obstacle, portals stay in free space),
pairwise portal capacity, the funnel's known-optimum on synthetic
corridors, and domain/funnel wall time on the ewod replay and nano
(slow tests print the table).

The comparison is per connection, single layer (its goal layer), against
`build_tile_graph(capacity="flow", strict=True)` with `exit_cap` for the
net's own copper. Measured 2026-10-18; both runs gave the same counts.

| Board | Connections | CDT portal sequence | Flow tile path | Terminal tile reads 0 lanes | CDT finds one there | Tile only | CDT build |
|---|---|---|---|---|---|---|---|
| qfp-plaza (B.Cu) | 55 | 54 (miss: N31) | 25 | 27 | 26 | 0 | 1.1 s |
| ewod-replay (goal layer) | 55 | 55 | 34 | 14 | 14 | 0 | 1.5 s |

These numbers use the stand-in obstacles and the class `s` described
above, not the exact kernel, so slice 6a re-measures them on the kernel.
Answer to the slice question: yes on the ewod replay. Wherever the tile
model walls a terminal in, the CDT still has a portal sequence (14/14).
On the QFP plaza member the CDT has one for 26 of the 27 walled-in
terminals. The tile model finds no path that the CDT lacks. Reachability
here is per net: the probe does not check that all nets fit together
within portal capacity. Slice 6a's negotiated congestion is that test.

Left in this slice: portal columns in slice 0's bench once the bench
lands. N31's miss is explained: the fixture's plaza source at (0, 9)
overlaps two unconnected QFP pads, so the terminal is buried in foreign
copper (`RegionGraph.shorted_by`), not cut off by portal capacity.

Owning files: `src/precis/pcb/topo.py`, `tests/test_pcb_topo.py`.

### Decisions 15–16 in the vector slices

Reto ruled on this section's questions on 2026-10-18 (vias ~18:5xZ, the
rest ~19:0xZ). Batch shove (Decision 15) and the place-route loop
(Decision 16) are designed in
[pcb-unified-place-route](pcb-unified-place-route.md): its cost, its
insertion-plus-descent shove (slice 5 there), and its round schedule.
This item adds one thing, the congestion signal V-overflow below, which
feeds that item's T7 ("router needs space"). Interactive push-and-shove
stays a Non-goal.

| Ruling | Where it lands | Owner |
|---|---|---|
| 15, vias and parts | the unified insertion + descent. Any via may move (router-placed, user-placed, stitching); footprint copper, through-hole pads included, never moves (Reto: "Any vias may be moved. The thp for footprints and any footprint specific things must remain.") | pcb-unified-place-route |
| 15, vias on the maze path | not built. The proposed V-shove post-pass is superseded by pcb-unified-place-route slice 5 (Reto: "unified is better, lets not waste tokens") | — |
| 16, the loop | the unified schedule's rounds (the `anneal_route_loop` role); this item supplies `vroute`/`topo` as library searches (its composition rows 3 and 5) | pcb-unified-place-route |
| 15–16, congestion signal | V-overflow below, the source of the unified T7 | this item |

**Where a wire must pass, today.** Slice 5's probe answers per net only
"is there a portal sequence at all": `RegionGraph.portal_path` is a
breadth-first search for the sequence with the fewest portals of capacity
≥ 1, one per net. It is not length-shortest, it offers no alternative
homotopy class, and it puts no demand on the portals it crosses. V-overflow
replaces that yes/no witness with a demand per portal.

#### V-overflow — fractional portal demand (with slices 6a/6b)

A portal is a gap between two named obstacles (`topo.Portal.ends`), so
congestion on it names the via or part that bounds the choke. A tile
boundary has no owner, so the tile model cannot do this.

Definition, per layer (Reto, ~19:0xZ):

1. **Candidates.** Each net `n` gets `K` candidate portal paths, one per
   homotopy class: the k-shortest distinct sleeves on the CDT dual
   (step 3, item 3), plus 6a's converged path once it exists. Path cost
   `cost_{n,i}` is its length plus the shared route cost (step 2).
2. **Fractional choice.** `p_{n,i} = softmax_i(−cost_{n,i} / T)`. Each
   net spreads one unit of demand over its candidates.
3. **Demand.** `D_e = Σ_n Σ_{i ∋ e} p_{n,i}` over portal `e`.
4. **Overflow.** `O_e = softplus_β(D_e − C_e)`, with `C_e = (L_e − s)/(w + s)`.
   This is `topo.portal_capacity` before the floor, so `C_e` is smooth in
   the positions of the two obstacles that bound `e`, and so is `O_e`. Its
   gradient pushes those two owners apart along the portal.
5. **Annealing.** `T` falls between rounds, so each net congeals onto one
   class: at `T → 0`, `p` is one-hot on the cheapest path and `D_e` is an
   integer count. This is deterministic annealing over the probabilistic
   congestion estimate, the smooth cousin of 6a's negotiated history
   cost. Candidates stay fixed within a round and are re-enumerated
   between rounds, because a homotopy class is discrete.

This item delivers `D_e`, `O_e`, and `∂(Σ_e O_e)` with respect to each
movable owner's position (via centre, instance pose), returned beside the
topology result. How T7 weighs it is pcb-unified-place-route's call.

Exit:
- The gradient passes the `differentiation` skill's three-stage check
  (directional, Taylor remainder, central differences on owner
  positions).
- At the lowest `T`, `D_e` equals the integer count of the chosen paths
  on every benchmark.
- On ewod replay, qfp-plaza and nano: total overflow is reported per `T`
  step, and every overflowing portal attributes to at least one owner.
- Fixed-pose boards report the gradient without moving anything.

Owning files: `topo.py`, `tests/test_pcb_topo.py`.

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

Only the SCP/HiGHS QP spacing cleanup is superseded by pcb-unified-place-route (Reto 2026-10-18; that spec's ERBS pass + descent); keep it only if it later measurably beats them on the bench. Funnel string-pulling, offsets and 45° conversion move into that spec's slice 4.

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

### Slice 10 — retire the maze (Decision 1) — DONE ahead of schedule

Landed in d42369679 (2026-10-20): the grid router was deleted outright
(`maze.py`, `globalplan.py`, the `corridor=`/`negotiate=` params, the
reorder passes) and `vroute` became the only router. The exit criteria
below were written for a gradual retirement; what remains as ongoing
exit discipline is the benchmark table holding its counts. The list of
items it retired, kept for the record:

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
- nano seed 1: routed ≥ 40, failed ≤ 24, `silk_missing` ≤ 35, `clearance`
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
- `pcb-global-plan-router.md` (deleted with the grid router; git history) — levels [A]/[B]/[C]; [B] moves to the CDT.
- `pcb-global-plan-probe.md` (deleted; git history) — tile-model evidence.
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
