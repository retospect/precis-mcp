---
status: draft
title: "pcb: unified place-and-route — one smooth cost over poses, labels, tracks and vias, annealed with discrete topology moves"
prio: high
model: opus
pillar: 3d-design
---

# pcb unified place-and-route

Source: Reto, 2026-10-18 (quoted below), on top of the vector-router
rulings of 2026-10-16/18. Review inputs: `perplexity-research:496658`
(read with `args={'no_fetch': True}`), the placer and router modules named
in §Current state, [pcb-vector-router](pcb-vector-router.md),
[pcb-global-plan-probe](pcb-global-plan-probe.md). This file proposes; it
does not edit the vector-router spec (§Composition names the slot).

## Reto's position

Verbatim, 2026-10-18 ~17:0xZ. Design input, not to be redesigned.

> "Ok I want to talk about place and route. I think we do it wrong. Here's
> the issue. We have untold degrees of freedom. We want to valid place, ie
> all the parts can be in a place and have a label. I would say that things
> that are densly connected should be close together, and optimized so the
> wires cross minimally, but that is secondary. We then try route. When we
> hit an issue, we go back and shift the placement around. And start over.
> I believe that we should be able, with a depth first approach, to mostly
> find a path. I'd take money out of it. I'd say we have rubber things (ie,
> cap is close to bypass pins). (x and y should be ... elasticish appart,
> with a few force fields. One force field is the connectivity graph (pull
> togehter) one is the "router needs more space" graph. We anneal the
> totality of placement (including labels) routing and so on in one smooth
> space. Routing is vectors on layers. Routing insertion can shove other
> wires if needed (including past vias and thps, hop over if needed).
> Labels are part of placement. So I think what I argue is we build force
> fields that shove and push towards rotation different parts. we have grid
> likings (prefer to rotate parts 0, then 180, then 90, then 45, then 30
> degree multiples as we have with lengths) (prefer 1cm raster, 5mm raster,
> 2mm raster, 1mm raster 0.5 etc., but similar parts in arrays are also
> likeable. ) Locked components remain locked. Use fable (devin fable) and
> some perplexity to review and discuss."

> "For the routing, I also want a way to represent this cost as a smooth
> curve. We want to be able to make small wiggles and changes to see what is
> a "better"solution than the previous one, so we can gradient descent the
> thing."

Earlier rulings relayed by the coordinator as `pcb-vector-router.md`
"Decisions (Reto 2026-10-18)" rows 13–16: vector first; the absolute
target is every net routed with 0 DRC and 100 % labels placed ("better
algo should not require any label missing. 100% is our quest"); batch
shove of parts and vias with gradual weights; a multi-iteration
place-route loop. Those rows are not in `pcb-vector-router.md` on
`origin/main` at 8b05fe3dc (only rows 1–12 and the 2026-10-18 nano-floor
note in slice 3); they are quoted here from the brief.

## Current state (verified on origin/main 8b05fe3dc)

| Claim | Verified | Where |
|---|---|---|
| Default pipeline: one anneal, then one maze route, no feedback | Yes. `pcb_route._dispatch` runs the joint place+sketch annealer (`optimize.OptimizeEngine`, move set `MoveKind`: TRANSLATE/ROTATE/SWAP/LAYER_ASSIGN/SIDE_FLIP/PLANE_*), then `realize()` once. `loop=N` (≤ `optimize.LOOP_MAX_ROUNDS` = 3, default rounds 2) is opt-in and swaps in `optimize.anneal_route_loop` (route → `apply_route_feedback` → anneal → route). | `src/precis/workers/job_types/pcb_route.py` docstring; `optimize.py` `RouteLoopConfig`, `anneal_route_loop` |
| Anneal cost is money + 100·risk; wirelength is chord area | Yes. `cost.evaluate_cost`: `total = money + config.risk_to_money * risk`, `CostConfig.risk_to_money = 100.0`; `routing_area_term` prices each segment's chord as panel area. **Addition:** `ROTATE` and `SIDE_FLIP` are provably cost-neutral — no term reads `inst_rot`; rotation is not optimised today. | `cost.py`; `optimize.py` comment above `_PLACEMENT_KINDS` ("ROTATE is cost-neutral, provably") |
| Courtyard clearance 0.40 mm | Yes. `ir.COURTYARD_CLEARANCE_MM = 0.40`, read by `optimize._placement_is_legal`, `cost`, `drc`, `check_run`, `session`. Legality is a hard predicate in the annealer, not a graded term. | `ir.py`, `optimize.py` |
| Maze: ~0.1 mm grid, 8-direction A*, via 3 mm, rip-up by re-ordering | Yes. Pitch = `clearance × realize._PITCH_PER_CLEARANCE` (2/3) capped by `maze.grid_for` (0.0675 mm on the ewod replay); `maze._STEPS` is 8 moves; weighted A* `HEURISTIC_WEIGHT = 1.15`; `VIA_COST_MM = 3.0`, `VIA_UNDER_BODY_COST_MM = 3.0`; rip-up is re-ordering in `realize._realize_maze` (maze docstring "Rip-up and retry"). | `maze.py`, `realize.py` |
| Only F.Cu and B.Cu are routable | **No.** Routable layers are the stackup's: `realize._signal_layers` → `ir.routable_layers` (per-layer routable flag/role). The ewod replay routes on In2.Cu + B.Cu; esp32c3 is four layers. Two-layer boards (nano) route F.Cu and B.Cu because that is their stackup. | `realize.py`, `ir.py` |
| Silk labels built at render/DRC time, not placed by the optimizer | Yes. `silk.build_silk` is a pure function of the settled `PcbIR`; the label spot is a candidate search (`LABEL_SPOT_ORDER`, `_outline_spot_candidates`, radial `_CANDIDATES`, `_inside_candidates`, else dropped → `silk_missing`). Glyphs are pinned to 0° (Reto, gr495394). Callers: `realize` (via keep-outs, off by default), `drc`, `check_run`, `handlers/pcb`. | `silk.py` docstring |
| Floors: nano seed 1 35/29, silk_missing 35; 8-seed mean 34.375, min 28 | Yes. `BASELINE_ROUTED = 35`, `BASELINE_FAILED = 29`, `BASELINE_DRC_ERRORS_BY_RULE["silk_missing"] = 35`, `NANO_MEAN_ROUTED_FLOOR = 34.375`, `NANO_SEED_ROUTED_MIN = 28`. | `tests/test_pcb_nano_usb_pd_testboard.py` |
| Vector router slices 0–1 landed, slice 2 WIP; VP probe 2cedf6b13 | **Not on main.** No `clearance.py`, `vroute.py`, `topo.py`, `tests/test_pcb_topo.py`, no `rules.required`/`clearance_by_layer`. They live in builder trees. The CDT-portal finding (14/14 ewod, 26/27 qfp-plaza, vs the tile model) is taken from the brief. | `ls src/precis/pcb`, grep |
| Tile-plan corridors hurt (38–47 vs 51/55) | Yes. | [pcb-global-plan-probe](pcb-global-plan-probe.md) Parts 3–4 |

Two facts the design builds on: the IR is already a rubber-band sketch
whose topology (L0–L2) is invariant under a placement perturbation (L3)
(`optimize.py` docstring, "One engine, not place-then-route"); and the
vector-router kernel (slice 2) is specified to expose
`distance(prim, prim)` and `gradient` analytically ("Better maths" table,
row "Signed distance, exact analytic").

## Critique of the starting take

| Coordinator's take | Verdict |
|---|---|
| Fully smooth placement as in ePlace/DREAMPlace; overlap as an electrostatic density field | Smooth x, y, θ: yes. Electrostatic density: **no at our size.** ePlace's FFT Poisson field is for 10⁵–10⁶ near-uniform cells; a board has 30–300 heterogeneous footprints and a hard 0.40 mm courtyard rule. Pairwise exact signed distance between courtyard polygons with a softplus penalty is cheaper, exact at convergence, and has a gradient. A density term buys nothing a pairwise term does not. |
| LSE/WA wirelength per net | Yes, on **rotated pad centres** (Cypress), never on centroids — that is what makes rotation a variable at all. Once tracks exist, the track elastic energy replaces it (§Cost, T1/T2). |
| RUDY for "router needs space" | **Replace.** RUDY smears demand over a bbox and ignores which airwires conflict; the survey says it misreads few-layer PCBs. The faithful push is the clearance gradient of the current tracks flowing into part poses, plus a chord-corridor term for nets not yet inserted (T7). `OptimizeEngine.apply_route_feedback`'s `route_demand` margin is the discrete ancestor. |
| Periodic wells, coarse to fine | Yes. With pitches 10/5/1/0.5 mm nested, the summed wells are deepest at coarse multiples by construction; 2 mm is not nested in 5 mm and needs its own weight. Same construction for rotation (T10) and segment angle (T11). |
| Array regularity as spacing variance | Yes (T12). Group membership is frozen before descent (a catalogue choice, not a variable). |
| Locked parts not variables | Yes; also `fixed='rot'` removes θ only, `fixed='both'` removes x, y, θ. |
| Augmented Lagrangian on overlap, then a small legalization | Yes. Legalization is the existing `_placement_is_legal` predicate plus the kernel; a result that is not legal after the ramp is a failed round under the never-worse ratchet, never a committed overlap. |
| Routing smooth only within a fixed topology; topology changes are discrete | Yes, and the survey, the vector-router doc and SURF all say so. The addition: **shove is not a separate discrete move.** Insert a path with reduced inflation, then descend all variables — the clearance gradient moves the neighbours. Discrete moves are only what descent cannot do: side swap, via hop, layer change, rip-up. |
| Rotation: smooth well plus discrete jumps | Yes. θ stays a continuous variable with wells; a 90°/180° jump is an SA move whose proposal is followed by descent (basin hopping). |
| Cost: weighted sum with penalties for unrouted nets and DRC | Smooth terms in the descent; unrouted count, via count and residual DRC are **discrete acceptance terms** of the annealer, never faked smooth (differentiation skill rule 2). |

## Design

### State

One vector `z` of continuous variables, packed per board:

| Block | Variables | Not a variable when |
|---|---|---|
| Part pose | `x_i, y_i, θ_i` per instance | `fixed='both'` (all), `fixed='rot'` (θ), `fixed='pos'` (x, y); edge-mated parts keep the edge constraint (`snap_to_edges`) |
| Label pose | `lx_i, ly_i` per label (glyph angle 0°, mirrored on bottom) | never; a part with no label has none |
| Track vertices | `v_{n,k} = (x, y)` per inserted segment, N vertices per segment (N fixed per segment, redundant vertices collapse on straight runs) | before insertion (T1 stands in) |
| Via position | `(x, y)` per via | — (existence is discrete) |

Discrete state, owned by the annealer: per-segment layer and via
existence, per-segment sidedness signature (which side of each obstacle
it passes — the homotopy class), side (`inst_bottom`), rotation well index
(which well θ sits in), label spot index, array groups, support groups.

### Cost (money out)

`E(z) = Σ_t λ_t T_t(z)` over the smooth terms below. Every term is a sum
of closed-form pair or per-object expressions, so the gradient is the sum
of analytic pair gradients (reverse mode by hand; cost independent of
variable count). `softplus_β(u) = log(1 + e^{βu}) / β`; `sd(a, b)` is the
kernel's signed distance; `req(a, b, L)` is `rules.required(...).warn_mm`
(vector slice 1). Pads are `R(θ_i)·p_local + (x_i, y_i)`, so every pad
term carries ∂/∂θ.

| # | Term | Formula | Variables | Smooth | Gradient / move |
|---|---|---|---|---|---|
| T1 | Connectivity pull (pre-insertion) | per net, WA wirelength on pad centres: `Σ x e^{x/γ} / Σ e^{x/γ} − Σ x e^{−x/γ} / Σ e^{−x/γ}`, same for y; γ annealed down | x, y, θ | yes | analytic WA derivative on pads, chain through R(θ) |
| T2 | Track elastic (post-insertion) | `Σ_segments Σ_k √(|v_{k+1} − v_k|² + ε²)`; endpoints are pads (functions of pose) and vias | vertices, vias, x, y, θ | yes | per-edge unit vector; replaces T1 for inserted nets |
| T3 | Copper clearance | `Σ_{(a,b) kernel pairs, foreign nets} softplus_β(req(a,b,L) − sd(a,b))²` over track–track, track–pad, via–anything, pad–pad (different footprints) on each layer | everything | a.e. (medial-axis set is measure zero) | kernel `gradient(prim, prim)` w.r.t. the primitive's defining points; STRtree-culled pair list rebuilt every K steps |
| T4 | Courtyard clearance | `Σ_{i<j} softplus_β(0.40 − sd(poly_i, poly_j))²` (`COURTYARD_CLEARANCE_MM`) | x, y, θ | piecewise (GJK/SAT witness pair) | witness-pair normal; same polygons `_placement_is_legal` tests |
| T5 | Board edge and keep-outs | `Σ_obj softplus_β(inset − sd(obj, outline))²` for parts, tracks, vias, labels | all | yes | outline edge normal |
| T6 | Rubber bands (support groups) | `Σ_{(p,q) ∈ support pairs} k·softplus(d(p,q) − d₀)²` between the cap's pad and the IC's pin, pads from `support_groups` | x, y, θ | yes | spring |
| T7 | Router needs space | for each not-yet-inserted segment with chord `c`: `Σ_{parts j near c} softplus_β(r_n − dist(courtyard_j, c))²`, weight `w_n` grows per failed insertion round; for inserted nets the force is T3's gradient on the parts bounding each pinched pair | x, y, θ | yes | point–segment distance gradient; `w_n` from the round counter (the `route_failure`/`route_demand` margins today) |
| T8 | Labels | `Σ_i a·|l_i − bl_i(pose)|² + Σ softplus_β(req_silk − sd(label_i, o))²` over o ∈ pads, vias, outlines, other labels, board edge (`_label_blocked`'s set) | lx, ly, and x, y, θ through `bl_i` | yes | box–shape distance; label box from `stroke_font.layout_text` extents |
| T9 | Grid wells | `Σ_i Σ_{p ∈ {10,5,2,1,0.5}} w_p (1 − cos(2π x_i / p))`, same in y; anchor point is pad 1 (open question 6); vias on their own pitch | x, y (parts, vias) | yes, non-convex | `w_p (2π/p) sin(2π x/p)`; `w_p` = 0 early, ramped late |
| T10 | Rotation wells | `Σ_i Σ_k w_k (1 − cos(k θ_i))`, k ∈ {1, 2, 4, 8, 12} for 0 > 180 > 90 > 45 > 30°, `w_1 > w_2 > w_4 > w_8 > w_12` | θ | yes, non-convex | `w_k k sin(kθ)` |
| T11 | Segment angle wells | `Σ_edges w_a (1 − cos(8 φ_e))`, φ_e the edge direction (`check_octilinear` house rule) | vertices, vias | yes | via atan2 derivative; arcs exempt |
| T12 | Array regularity | per frozen group of same-footprint parts in a row: `Var(Δ_i)` over consecutive spacings + `Σ (1 − cos(θ_i − θ_j))` | x, y, θ | yes | linear in Δ |

Discrete acceptance terms (annealer only, never in the gradient):
unrouted segment count × M, via count × `VIA_COST_MM`-equivalent, kernel
violations after snap × M', label dropped × M''. Money terms
(`board_area`, `layer_count`, `via_count` USD, `routing_area`) leave the
objective; `pcb_cost` keeps reporting them (open question 1).

Weights `λ_t` are schedule-dependent (below), not board-dependent. No
term is normalised to USD.

### Optimizer

Basin hopping over `E`: an SA loop whose every proposal is followed by a
bounded descent, accepted on the descended energy plus the discrete
acceptance terms. "Anneal the totality" is this loop; the smooth phase is
its inner step.

**Smooth step.** Adam or L-BFGS on `z` with the current `λ`, step length
capped at `min(req)/4` per variable (a track vertex never crosses an
obstacle in one step), K steps per call. After each step the sidedness
signature is recomputed for moved segments; a change is a rejected step
(topology is the annealer's).

**Discrete move set** (each: propose, descend K steps, accept/reject):

| Move | What | Replaces |
|---|---|---|
| ROTATE_JUMP | θ_i += 90° or 180°, then descend | `MoveKind.ROTATE` |
| SIDE_SWAP | flip one segment's side of one obstacle (re-insert with the other sidedness bit) | `seg_side` |
| VIA_HOP | split a segment at a vertex, change layer on one side; or remove a via | the maze's layer change |
| LAYER_SWAP | move a whole segment to another routable layer | `MoveKind.LAYER_ASSIGN` |
| RIP_REINSERT | remove the most-stressed segment(s) in a window, re-insert after the stuck one | `realize._realize_maze` re-ordering |
| LABEL_JUMP | move a label to the next spot in `LABEL_SPOT_ORDER`, then descend | the silk candidate search |
| SWAP, SIDE_FLIP, PLANE_* , OUTLINE_GROW, LAYER_ADD | as today (`MoveKind`), capped; priced only if open question 1 says so | — |

**Insertion ("depth-first find a path").** Segments ordered
most-constrained first (shortest slack, highest fanout). Each is found by
search on the current geometry — the vector router's tangent graph
(slice 3 `vroute`) or the CDT dual (slice 5 `topo`) — with obstacle
inflation at a fraction `α < 1` of `req` so a path exists where the full
inflation would wall it in; the resulting overlap is T3 stress, and the
descent that follows is the shove (Reto: "insertion can shove other
wires"). If descent cannot zero the stress, the annealer tries SIDE_SWAP,
VIA_HOP, then RIP_REINSERT on the 1-hop neighbours, depth-first with a
bounded backtrack budget per segment; a segment that exhausts the budget
stays uninserted and feeds T7 in the next round. Nothing already inserted
is frozen — that is the difference from the maze and from slice 3's
post-pass.

**Schedule** (one run):

1. **Spread.** Variables: poses, labels. Terms: T1, T4, T5, T6, T8, T12;
   λ_T4 ramped (augmented Lagrangian) until courtyard stress is 0; wells
   off; SA temperature high for ROTATE_JUMP/SWAP.
2. **Insert.** Segments inserted in order; after each, K descent steps
   on all variables (T2, T3 replace T1 for inserted nets; T7 for the
   rest). One pass is a round; rounds repeat while uninserted segments
   remain and the budget holds (the `anneal_route_loop` role).
3. **Harden.** β up, λ_T3 up, wells on (T9 coarse → fine, T10, T11),
   temperature down. Ends with the hard snaps: θ to its well, part origin
   to the finest active raster, segment angles to 45°, labels to spots.
4. **Verify.** Kernel DRC (`drc.clearance_violations_naive` stays the
   independent oracle), `connectivity.net_islands`, label coverage. A
   residual goes back to step 2 for the offending window, with a budget;
   then the never-worse ratchet decides whether anything is written.

Where "depth-first" fits: step 2's insertion order and the bounded
backtrack over SIDE_SWAP/VIA_HOP/RIP_REINSERT for a stuck segment. Where
"gradient descent" fits: every K-step descent, in all phases. Where
"anneal" fits: the acceptance of each move on the descended energy.

### Derivative route (per the `differentiation` skill)

Many variables into one scalar ⇒ reverse mode. All terms are sums of
closed-form pair expressions, so the gradient is assembled by hand in
numpy/numba (the repo's existing stack; no JAX/torch core dependency;
vector Decision 10 keeps GPU out). Non-smooth pieces are smoothed (`|·|` →
`√(·² + ε²)`, max → softplus, snap → wells) or frozen (group membership,
layer, via existence, sidedness). The kernel's `gradient` is the one
shared derivative of `sd`. Each term ships with the three-stage gradient
test: directional dot-product, Taylor remainder halving, central finite
differences on a few variables at several steps (`structsolve/simp.py`
pattern). Finite differences are a test, never the production gradient.

### Composition with the vector router slices

| Vector slice | Reused as | Order change |
|---|---|---|
| 1 `rules.required` | `req(a, b, L)` in T3, T8 | none; prerequisite |
| 2 clearance kernel | `sd`, `gradient`, STRtree pair culling for T3/T4/T5/T8 | none; prerequisite. The kernel API must expose the derivative of `sd` w.r.t. each primitive's defining points (the "Better maths" row already names `gradient`) |
| 3 `vroute` tangent graph | the insertion search, with inflation `α·req` | the post-pass stays as specced for the maze path; the unified path calls the same search as a library |
| 4 shared route cost | T2 layer multipliers and direction penalty on segment edges | none |
| 5 CDT portals | insertion search alternative and the SIDE_SWAP candidate generator (the sleeve on the other side of a portal) | none |
| 6a/6b topology assignment | the topology oracle only when insertion + backtrack fails on a benchmark; 6b (LP) stays optional | proposed: 6b waits for the unified path's benchmark numbers |
| 7 SCP cleanup (HiGHS QP) | overlaps with the descent within a topology (T2 + T3 + T11) | **one formulation should win** (open question 7); proposal: the descent, since it also moves parts and labels |
| 10 retire the maze | unchanged exit criteria | the unified path is a second candidate for the default beside `router='topo'` |

Slot: a new item in parallel with vector slices 3–5, consuming 1–2,
landing behind an opt-in `router='field'` (name open) until §Measures
says otherwise. `optimize.OptimizeEngine` keeps its hard-locality rule:
the descent is a separate module (`src/precis/pcb/field.py`, new) called
from the engine's move loop, and the engine's per-move delta cost is not
touched.

## Measures and exit criteria

Per board, every run, written by the vector slice 0 harness
(`scripts/pcb-route-bench`): segments inserted / total, kernel DRC errors
and warns by rule, labels placed / total, vias, track length, wall time,
seconds per net.

Exit (the thread's, not a slice's): on nano (8 seeds), r2, dogfood-b (the
two named by the coordinator's brief; neither is a test fixture on main —
slice 0 of the harness has to pin them as snapshots), the ewod replay
(55) and the qfp-plaza member (55): **100 % segments routed, 0 DRC
errors, 100 % labels placed**, with runtime reported against vector
Decision 8 (quality first; the job wall is the only hard cap). Until then
every slice holds the existing floors (nano mean ≥ 34.375, seed min ≥ 28,
`silk_missing` ≤ 35, `clearance` warns ≤ 9, ewod ≥ 51, qfp ≥ 53) and the
never-worse ratchet.

## Slices

Each lands alone, with its test; smallest useful first.

| # | Slice | Test |
|---|---|---|
| 0 | `field.py` skeleton: state packing, term interface `(value, grad)`, gradient test harness; T1 and T4 implemented | `tests/test_pcb_field.py`: dot-product, Taylor, FD on random boards for T1, T4; rotation gradient non-zero |
| 1 | Placement polish: after `optimize()`, descend x, y, θ on T1 + T4 + T5 + T6 with λ_T4 ramp; accept only if legal and E lower | nano 8 seeds and ewod not worse; legality predicate holds; synthetic 3-part board converges to the known optimum |
| 2 | Wells T9, T10; θ becomes a real variable (ROTATE stops being cost-neutral) | synthetic board lands on 10 mm multiples and 0°/180° in preference order; nano floors |
| 3 | Labels as variables (T8) with LABEL_JUMP fallback; `silk.build_silk` reads the stored label pose | `silk_missing` on nano ≤ 35 and reported; 100 % on a synthetic board the candidate ladder fails |
| 4 | Track relaxation within topology (T2, T3, T11, snap, kernel verify) on the maze's or `vroute`'s output; needs vector slice 2 | DRC warns not up, track length down on nano/ewod; synthetic pinched corridor opens when a part is free |
| 5 | Shove by descent: insertion with `α·req`, joint descent on parts + tracks + vias + labels; T7 | synthetic two-wire corridor routes both; ewod ≥ 51 reported, target 55 |
| 6 | Discrete move set + basin hopping + the full schedule, opt-in `router='field'` | the five boards' table; becomes default when it ties or beats on all five |
| 7 | T12 arrays and T6 from `support_groups`; array groups frozen from footprint + value + refdes run | ewod array spacing variance 0; nano decoupling caps within d₀ |
| 8 | Exit run against §Measures; retire the superseded path components the table names | the thread's exit criteria |

## Risks

| Risk | Mitigation |
|---|---|
| Non-convex wells and penalties trap descent in a poor basin | wells only in Harden; basin hopping with ROTATE_JUMP/SWAP; `NANO_SEEDS` already measure multi-start |
| A descent step tunnels a vertex through a thin obstacle (topology change unnoticed) | step cap `min(req)/4`; sidedness recomputed per step, change = reject; capsules not sampled points |
| Gradient evaluation cost: ~10⁵ culled pairs × 10³–10⁴ steps | numba pair kernels; pair list refreshed every K steps; measure in slice 0/1 before any schedule tuning |
| Smooth zero ≠ DRC zero (finite β), and the 45° snap re-opens violations | T11 before snap; kernel verify after; the residual goes back to Insert with a budget; the ratchet refuses a worse board |
| 100 % labels is infeasible on a dense board without growing it | OUTLINE_GROW as a capped discrete move; open question 4 |
| Two cleanup formulations (vector slice 7 SCP vs this descent) | open question 7; one wins before either lands as default |
| The hard-locality rule of `OptimizeEngine` vs a global descent | descent lives in `field.py`, called as one move; the engine's delta cost untouched |
| Hand-written gradients drift from the terms | the three-stage gradient test is a gate on every term |
| Rounds of insert/descend exceed the job wall on taz15-size boards | budget per segment and per round; the best round is restored (`anneal_route_loop` already does this) |
| Slices 0–3 land before the kernel exists and build a second distance code path | T4/T5/T8 use shapely polygon distance until slice 2 lands, then switch; the oracle test pins agreement |

## Open questions for Reto

1. Money fully out of the objective, or kept as a reporting number with
   `OUTLINE_GROW`/`LAYER_ADD` still capped by `expansion_caps`?
2. During descent, any-angle with a final 45° snap (T11 + snap), or 45°
   wells from the start? Does the vector slice 11 any-angle option become
   this path's default?
3. Track geometry as explicit vertex variables (proposed) or implicit
   string-pulling per topology?
4. When a label has no legal spot: grow the outline, spread the parts, or
   accept a `covered` label? Is bottom-left (gr495394) a hard preference
   or a well?
5. Rotation wells: are 30° multiples allowed for every part or only for
   passives? Weight order 0 > 180 > 90 > 45 > 30 as stated?
6. Raster anchor: part origin (today's `_preferred_snap_grid_mm` snaps
   the centre) or pad 1 centre (proposed, probing)?
7. Vector slice 7 (SCP QP per ribbon) vs the descent in slice 4 here —
   one formulation; which?
8. Gates: never-worse per slice as today, 100 % only as the thread exit?
9. Dependency: hand-written numpy/numba gradients (proposed) or a CPU
   autodiff library?
10. Default switch rule: `router='field'` becomes default when it ties or
    beats on all five boards (the vector slice 7 rule)?

## Literature

From `perplexity-research:496658` unless marked; links unverified,
import with `put(kind='paper')` before citing in a draft.

- ePlace: Lu et al., TODAES 2015 — https://cseweb.ucsd.edu/~jlu/papers/eplace-todaes14/paper.pdf ; ePlace-3D — https://cseweb.ucsd.edu/~jlu/papers/eplace-3d-ispd16/paper.pdf
- RePlAce — https://github.com/wisdom0530/RePlAce
- DREAMPlace: Lin et al., DAC 2019 — https://yibolin.com/publications/papers/PLACE_ICCAD2019_Li.pdf ; https://github.com/limbo018/DREAMPlace
- Cypress (PCB, orientation-aware WA, net-crossing term, Gumbel-Softmax rotation): Zhang et al., ISPD 2025, doi 10.1145/3698364.3705346
- RUDY: Spindler & Johannes, DATE 2007 — https://dl.acm.org/doi/10.5555/1266366.1266632 ; OpenROAD routability loop — https://openroad.readthedocs.io/en/latest/main/src/gpl/README.html
- SURF rubber-band routing: Dai, Dayan, Staepelaere, DAC 1991, doi 10.1145/127601.127622; UCSC reports — https://tr.soe.ucsc.edu/sites/default/files/technical-reports/UCSC-CRL-93-04.pdf
- TopoR — https://en.wikipedia.org/wiki/TopoR
- KiCad PNS shove — https://docs.kicad.org/doxygen/classPNS_1_1SHOVE.html ; https://docs.kicad.org/doxygen/pns__shove_8cpp_source.html
- gEDA PCB toporouter — https://fossies.org/linux/pcb/doc/pcb.html
- Differentiable global routing (Gumbel-Softmax over candidate trees) — https://proceedings.neurips.cc/paper/2021/file/898aef0932f6aaecda27aba8e9903991-Paper.pdf
- NS-Place net separation — https://ar5iv.labs.arxiv.org/html/2503.11674
- TrajOpt: Schulman et al., IJRR 2014, doi 10.1177/0278364914528132; CHOMP: Ratliff et al., ICRA 2009, doi 10.1109/ROBOT.2009.5152817 (both already in `pcb-vector-router.md`)
- Basin hopping (not from the survey): Wales & Doye, J. Phys. Chem. A 1997, doi 10.1021/jp970984n

## Related

- [pcb-vector-router](pcb-vector-router.md) — kernel, `vroute`, `topo`; the slot above.
- [pcb-global-plan-probe](pcb-global-plan-probe.md) — why hard corridors lose.
- `optimize.anneal_route_loop` (the shipped pcb-anneal-closes-router-loop item) — the loop this replaces.
- `docs/backlog/threads/pcb-platform.md` Horizon.
