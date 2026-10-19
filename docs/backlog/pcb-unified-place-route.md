---
status: draft
title: "pcb: unified place-and-route — one piecewise-smooth energy over poses, labels, taut-band tracks and vias, annealed with discrete topology moves"
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

**Acceptance sanity board:** the 100 x 100 mm four-header board
(`src/precis/pcb/sanity_board.py`, tests `tests/test_pcb_sanity_board.py`) — 20/20
nets and 0 DRC errors on every variant is the floor; iterate with
`uv run python -m precis.pcb.bench sanity [variant ...]`.

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
| LSE/WA wirelength per net | Yes, on **rotated pad centres** (Cypress), never on centroids — that is what makes rotation a variable at all. Once tracks exist, the band length energy replaces it (§Cost, T1/T2). |
| RUDY for "router needs space" | **Replace.** RUDY smears demand over a bbox and ignores which airwires conflict; the survey says it misreads few-layer PCBs. The faithful push is the band tension and clearance gradient of the current tracks flowing into part poses, plus a chord-corridor term for nets not yet inserted (T7). `OptimizeEngine.apply_route_feedback`'s `route_demand` margin is the discrete ancestor. |
| Periodic wells, coarse to fine | Yes. With pitches 10/5/1/0.5 mm nested, the summed wells are deepest at coarse multiples by construction; 2 mm is not nested in 5 mm and needs its own weight. Same construction for rotation (T10) and segment angle (T11). |
| Array regularity as spacing variance | Yes (T12). Group membership is frozen before descent (a catalogue choice, not a variable). |
| Locked parts not variables | Yes; also `fixed='rot'` removes θ only, `fixed='both'` removes x, y, θ. |
| Augmented Lagrangian on overlap, then a small legalization | Yes. Legalization is the existing `_placement_is_legal` predicate plus the kernel; a result that is not legal after the ramp is a failed round under the never-worse ratchet, never a committed overlap. |
| Routing smooth only within a fixed topology; topology changes are discrete | Yes, piecewise smooth (kinks where band contacts change); the survey, the vector-router doc and SURF all say so. The addition: **shove is not a separate discrete move.** Insert a path with reduced inflation, then descend all variables — band tension and clearance move the neighbours. Discrete moves are only what descent cannot do: side or order swap, via jump, layer change, rip-up. |
| Rotation: smooth well plus discrete jumps | Yes. θ stays a continuous variable with wells; a 90°/180° jump is an SA move whose proposal is followed by descent (basin hopping). |
| Cost: weighted sum with penalties for unrouted nets and DRC | Smooth terms in the descent; unrouted count, via count and residual DRC are **discrete acceptance terms** of the annealer, never faked smooth (differentiation skill rule 2). |

## Design

### Problem class

Mixed-integer, nonsmooth bilevel optimisation. Outer level, discrete
topology: layer per segment, homotopy class (side of each obstacle), band
order around each obstacle, which vias exist, rotation well, label spot.
Inner level, geometry: each segment is the shortest path of its class (a
taut band, below), a value function of part, via and obstacle positions.
Reduced into one energy `E(z)` over the continuous variables; the discrete
state is searched by the annealer.

`E` is piecewise smooth, not smooth. Gradients kink where a band gains or
loses a contact, and signed distance is itself nonsmooth on the medial axis
(clearance uses a soft-min over nearby features). The local step is a
trust-region method that takes Clarke subgradients, with gradient sampling at
detected contact changes; plain Adam stalls at kinks.

Complexity: one band in a fixed class is linear time; a fixed sketch is
polynomial (SURF); choosing non-crossing routes for many nets is NP-hard.
There is no global guarantee. SA's guarantee needs logarithmic cooling; ours
is a heuristic, hence the never-worse gate (§Measures).

### State

One vector `z` of continuous variables, packed per board:

| Block | Variables | Not a variable when |
|---|---|---|
| Part pose | `x_i, y_i, θ_i` per instance | `fixed='both'` (all), `fixed='rot'` (θ), `fixed='xy'` (x, y); edge-mated parts keep the edge constraint (`snap_to_edges`) |
| Label pose | `lx_i, ly_i` per label (glyph angle 0°, mirrored on bottom) | never; a part with no label has none |
| Via position | `(x, y)` per via (a bead on its bands) | footprint THT holes and footprint-specific features stay fixed; every other via may move |

Tracks are not variables. Each segment is a taut band (below); its shape is
derived from the obstacle, part and via positions in `z`.

Discrete state, owned by the annealer: per-segment layer, via existence, the
segment's homotopy class (side of each obstacle), the cyclic **order** of
bands around each obstacle (and via, Steiner point), side (`inst_bottom`),
rotation well index, label spot index, array groups, support groups
(including the chosen ground pin of each pair, below). Vias are through-only
with antipads (gr495289 c9); pads are hard via keep-outs (barrier in the cost
plus a final check).

### Taut bands

Each segment is the shortest path in its homotopy class around obstacles
inflated by `w/2 + clearance`: the funnel algorithm on the CDT, the SURF /
TopoR rubber-band sketch. Tracks have no variables of their own; the band's
length is a value function of the obstacle, part and via positions. Its
gradient with respect to those positions is the band tension at the contact
points (envelope theorem / Danskin): the squeeze of a band on the obstacle it
wraps.

Band–band spacing has no such gradient. Neighbouring ordered bands act as
offset obstacles for each other (offset = half widths + clearance), and
mutually offset bands form a fixed point, not a differentiable term. It is
solved by the ERBS joint spacing pass (SURF's extended rubber-band sketch),
run after each topology change and after each K descent steps. The pass's
reaction on parts and vias is the force descent sees. "Insertion can shove
other wires" means, mechanically: insert, re-solve ERBS, then descend on
parts and vias. Changing the order or a side is a discrete move.

The final shape is a band → octilinear **conversion** (funnel → tangent-arc →
45°), not a snap of variables. The explicit-vertex descent (vertices as
variables) survives only as a fallback for the final polish if the conversion
is messy.

### Cost (money out)

`E(z) = Σ_t λ_t T_t(z)` over the piecewise-smooth terms below. Every term is
a sum of closed-form pair, per-band or per-object expressions; gradients are
assembled by hand (reverse mode; cost independent of variable count).
`softplus_β(u) = log(1 + e^{βu}) / β`; `sd(a, b)` is the kernel's signed
distance, with a soft-min over nearby features where it would sit on the
medial axis; `req(a, b, L)` is `rules.required(...).warn_mm` (vector slice
1). Pads are `R(θ_i)·p_local + (x_i, y_i)`, so every pad term carries ∂/∂θ.

| # | Term | Formula | Variables | Smoothness | Gradient / move |
|---|---|---|---|---|---|
| T1 | Connectivity pull (pre-insertion) | per net, WA wirelength on pad centres: `Σ x e^{x/γ} / Σ e^{x/γ} − Σ x e^{−x/γ} / Σ e^{−x/γ}`, same for y; γ annealed down. Supply, ground and pour nets are excluded (their tree term is their only pull, see below) | x, y, θ | smooth | analytic WA derivative on pads, chain through R(θ) |
| T2 | Band length (post-insertion) | `Σ_segments L_band(class; ends, inflated obstacles)`, the taut-path length times a per-layer multiplier (a small extra on pour layers); ends are pads (functions of pose) and vias | x, y, θ, vias | piecewise (kinks where a contact appears or vanishes) | band tension at each contact (unit-vector difference at the contact vertex) onto the touched obstacle/part/via; replaces T1 for inserted nets |
| T3 | Copper clearance | band–band: the ordering + offset model, solved by the ERBS pass (not differentiated; any residual shortfall `softplus_β(req − gap)²` is stress for acceptance); kernel pairs `Σ softplus_β(req(a,b,L) − sd(a,b))²` for the non-band pairs (track–pad, via–anything, pad–pad of different footprints) on each layer | everything | piecewise | ERBS reaction force on the parts and vias; kernel `gradient(prim, prim)` for non-band pairs; STRtree-culled pair list rebuilt every K steps. The kernel signed distance also runs in Verify for all pairs |
| T4 | Courtyard clearance | `Σ_{i<j} softplus_β(0.40 − sd(poly_i, poly_j))²` (`COURTYARD_CLEARANCE_MM`); courtyards on opposite sides do not interact (cross-side overlap is legal) | x, y, θ | piecewise (GJK/SAT witness pair) | witness-pair normal; same polygons `_placement_is_legal` tests |
| T5 | Board edge and keep-outs | `Σ_obj softplus_β(inset − sd(obj, outline))²` for parts, vias, labels and bands; pad keep-outs for vias are a barrier | all | piecewise | outline edge normal; T5 holds vias in |
| T6 | Rubber bands (support groups) | `Σ k·softplus(d(p,q) − d₀)²` over a cap's pads and its IC pins; the pair is (supply pin, ground pin), see §Support pairs. Pre-insertion proxy: T14 replaces it once the cap's bands exist, and T6 keeps only the d₀ ceiling | x, y, θ | smooth | spring |
| T7 | Router needs space | for each not-yet-inserted segment with chord `c`: `Σ_{parts j near c} softplus_β(r_n − dist(courtyard_j, c))²`, weight `w_n` from congestion prices (below); for inserted nets the force is the band tension and T3's gradient on the parts bounding each pinched pair | x, y, θ | piecewise | point–segment distance gradient; prices from the round loop (the `route_failure`/`route_demand` margins today) |
| T8 | Labels | `Σ_i a·|l_i − bl_i(pose)|² + Σ softplus_β(req_silk − sd(label_i, o))²` over o ∈ pads, vias, outlines, other labels, board edge (`_label_blocked`'s set); bottom-left (gr495394) is the well `bl_i`, a preference, not a constraint | lx, ly, and x, y, θ through `bl_i` | piecewise | box–shape distance; label box from `stroke_font.layout_text` extents |
| T9 | Grid wells | `Σ_i Σ_{p ∈ P_i} w_p (1 − cos(2π x_i / p))`, same in y, `P_i = {10,5,2,1,0.5} ∪ {the part's own pitch}`, the latter from `_preferred_snap_grid_mm` (which picks the pitch); anchor is the part **centre** via `snap_to_preferred`, not pad 1, so flipped and rotated parts stay aligned; vias on their own pitch, active in Harden | x, y (parts, vias) | smooth, non-convex | `w_p (2π/p) sin(2π x/p)`; `w_p` = 0 early, ramped late |
| T10 | Rotation wells | `Σ_i Σ_k w_k (1 − cos(k θ_i))`, k ∈ {1, 2, 4, 8, 12, 24} for 0 > 180 > 90 > 45 > 30 > 15°, `w_1 > w_2 > w_4 ≫ w_8 > w_12 > w_24` (strong preference for multiples of 90; k=24 smallest); every part | θ | smooth, non-convex | `w_k k sin(kθ)` |
| T11 | Segment angle wells | `Σ_edges w_a (1 − cos(8 φ_e))`, φ_e the edge direction between band contacts. Off during descent; ramped in Harden, then the band → octilinear conversion (funnel → tangent-arc → 45°). Output default stays octilinear (`check_octilinear`); any-angle output is a per-board option. No per-layer preferred direction | x, y, θ, vias (through the contacts) | smooth | atan2 derivative; arcs exempt |
| T12 | Array regularity | per frozen group of same-footprint parts in a row: `Var(Δ_i)` over consecutive spacings + `Σ (1 − cos(θ_i − θ_j))` | x, y, θ | smooth | linear in Δ |
| T13 | Bend rule | at every band vertex with deflection `δ`: `softplus_β(δ − π/2)²`, so no turn sharper than 90° (no acute/acid-trap bends). On octilinear output finished bends are 45° or 90° only (interior angle 135° or 90°); on any-angle boards turns are ≤ 90°. Active from the start, on every layer; after the conversion a hard check, residuals go back like a DRC violation | x, y, θ, vias (through the contacts) | piecewise | deflection-angle derivative |
| T14 | Decoupling loop (PDN) | `E_PDN = α·Σ I²R + β·Σ_loops L_loop·I²` (both energy-like; `R = ρℓ/(w t)` per supply segment; weights are schedule-dependent, or each term is normalised by a per-board reference). A loop is IC supply pin → trace → cap supply pad → cap → cap ground pad → trace → IC ground pin → (inside the chip, fixed by the footprint) back. `L` for a trace over its return: `μ0·ℓ·h/w`, `h` the dielectric height to the return. The preferred pairing is stacked: supply and return on adjacent layers, one over the other, which is the `μ0·ℓ·h/w` case. Side-by-side on one layer is the fallback (supply and return as neighbouring traces, no plane): coplanar strips, `L = μ0·ℓ·K(k)/K(k')` with `k = g/(g+2w)`, `g` the gap and `w` the width, evaluated in closed form by Hilberg's approximation, `K(k)/K(k') ≈ (1/π)·ln(2(1+√k)/(1−√k))` (one log and one square root per segment pair, analytic gradient). For `g ≫ w` this tends to `(μ0·ℓ/π)·ln(4s/w)`, `s` the centre spacing. Check both against a field solver in slice 7. Plus a per-via inductance (~0.5–1 nH at 1.6 mm) | x, y, θ, vias (through the band shapes) | piecewise | length/height/width derivative through pad and via positions; see §Support pairs |
| T15 | Return-path continuity | no fast signal crosses a gap or slot in its adjacent ground. Penalty on the signal band's length over a slot in the ground pour/plane of the neighbouring layer | x, y, θ, vias (through the bands and the slot's bounding obstacles) | piecewise | pushes the signal off the slot, or the obstacle that makes the slot, away |
| T16 | Bundling | smooth pair attraction between same-layer segments ("keep it together"), leaving large free regions; the pour-layer length penalty is a T2 multiplier, not here | x, y, θ, vias (through the bands) | piecewise | pair distance |
| T17 | Pour neck (Verify only) | once the pour has run: `Σ softplus_β(w_min(net) − neck)²` per pour net, `w_min` from IPC-2152 width-for-current; thermal-relief spokes count toward a power pad's neck, and a solid connection is allowed for power pads | x, y, θ, vias (through the bounding bands) | piecewise | force pushing the bounding tracks apart; the window loops back |

**Support pairs (T6, T14).** `support_groups` extends from cap → one pin to
cap → (supply pin, ground pin) pair. The ground pin giving the smallest loop
is chosen once at start; `PAIR_SWAP` is a discrete move used only when
stuck: T14's loop area is not reduced over N descents, or the cap violates its d₀ ceiling. Pins far apart: the in-chip segment is fixed; the term pulls the cap
toward the supply pin with its ground pad dropping by a short via to the
ground pour, so the return closes under the chip. Caps may sit on the
opposite side under the part (courtyard overlap across sides is legal; still
no via-in-pad, gr496850; vias sit just beside the pads and count in the
loop). The term decides per board; under-part wins only when much closer
(QFN/BGA). On 2-layer boards the term co-routes supply over its return (TI:
power directly over ground, length:width ≤ 3:1). Full PDN impedance versus
frequency is sign-off only, not in the optimiser.

**Ground, supply and pours.** During routing, ground, every supply net
(3V3, 5V, VBUS, ...) and every pour net keeps a **tree** in the cost (the
"supply system tree") so it stays connected while things move; at the end it
pours or grows per `grow_to_fill`. Intended consequence: a supply tree's
length term pulls the parts of one supply domain together, so 3.3 V parts
cluster and 5 V parts cluster. These nets are excluded from T1: the tree
term is their only pull, with a per-net weight, ground lowest (it spans every
part and so clusters nothing). At the end the
pour fills and re-merges freely (small re-merged loops lower inductance;
ground-loop problems are large cable loops, not a pour meshing on one
board). Requirement: each pour net is one connected piece; no unintended
islands; no neck below its minimum width; enough parallel vias per layer
change (from current); thermal-relief spokes count toward a power pad's neck
(solid connection allowed for power pads); no slivers or acute corners below
fab minimum. Per-net setting `grow_to_fill` (new per-net setting): default on for ground and for
nets with current > 0.5 A; those nets become pours in Verify. Minimum neck
width per net is the IPC-2152 width for its current (per-net current is
already known, 08aae1f2b), not a fixed multiple.

**T7's congestion source (Reto 2026-10-18 ~19:0xZ).** For nets not yet
inserted, the portal demand is fractional: each net spreads one unit over its
candidate portal paths (homotopy classes) with `softmax(−cost/T)` (soft-min
error ≤ T·log K for K candidates), and `T` anneals so each net congeals onto
one path. Overflow per portal is `softplus(demand − capacity)` with capacity
smooth in the positions of the two obstacles that bound the portal. Instead
of growing a weight each round, **Lagrangian prices** sit on portals and
corridors: a price rises while demand exceeds capacity, and each net routes
against the prices. A multicommodity-flow LP relaxation optionally gives a
lower bound, reported as "how close to achievable". The definition and its
gradient with respect to via and part positions are
[pcb-vector-router](pcb-vector-router.md) §Decisions 15–16 in the vector
slices, V-overflow; that output supplies T7's force on the parts and vias
that bound a choke. The chord term above is the fallback before the CDT
exists.

Discrete acceptance terms (annealer only, never in the gradient): unrouted
segment count × M, islands and pour failures × M, via count ×
`VIA_COST_MM`-equivalent, kernel violations after snap × M', label dropped ×
M''. Money (`board_area`, `layer_count`, `via_count` USD, `routing_area`) is
out of the objective; `pcb_cost` keeps reporting it. A smooth "shrink-wrap"
outline-area term is a parked option, not in scope.

Weights `λ_t` are schedule-dependent (below), not board-dependent. No term
is normalised to USD.

### Optimizer

Basin hopping over `E`: an SA loop whose every proposal is followed by a
bounded descent, accepted on the descended energy plus the discrete
acceptance terms. "Anneal the totality" is this loop; the smooth phase is its
inner step.

**Local step.** Trust-region descent on `z` with the current `λ`: Clarke
subgradients, gradient sampling at detected contact changes, K steps per
call, trust radius capped at `min(req)/4` per variable. Band shapes are
recomputed from the positions each step (a funnel pass per moved band); order
and homotopy class change only by annealer moves.

**Discrete move set** (each: propose, settle with a short descent, accept or
reject on the settled energy):

| Move | What | Replaces |
|---|---|---|
| ROTATE_JUMP | θ_i += 90° or 180°, then descend | `MoveKind.ROTATE` |
| SIDE_SWAP | flip one band's side of one obstacle (re-thread with the other side, then settle) | `seg_side` |
| ORDER_SWAP | swap two bands' cyclic order around an obstacle, via or Steiner point | — |
| VIA_JUMP | move a via across an obstacle: re-thread with the other side, then settle | — |
| VIA_INSERT / VIA_REMOVE | split a band at a point and change layer on one side (insert), or merge (remove) | the maze's layer change |
| LAYER_SWAP | move a whole segment to another routable layer | `MoveKind.LAYER_ASSIGN` |
| RIP_REINSERT | remove the most-stressed segment(s) in a window, re-insert after the stuck one | `realize._realize_maze` re-ordering |
| LABEL_JUMP | move a label to the next spot in `LABEL_SPOT_ORDER`, then descend | the silk candidate search |
| PAIR_SWAP | change a decoupling cap's ground pin; only when stuck (loop area not reduced over N descents, or the d₀ ceiling violated) | — |
| SWAP, SIDE_FLIP, PLANE_* | as today (`MoveKind`) | — |
| OUTLINE_GROW, LAYER_ADD | as today, capped by `expansion_caps`. Proposed only when a round ends with unrouted segments, dropped labels or pour failures after the budget; accepted only if they remove failures (acceptance terms above) | — |

Vias move continuously (beads on their bands): band tensions pull, clearance
pushes, T5 holds them in, T9's via-grid well acts in Harden. Crossing an
obstacle is a discrete jump.

**Labels with no legal spot.** First T8's force spreads the parts; then
LABEL_JUMP to the next spot; then the label counts as a failure that may
trigger OUTLINE_GROW within caps. A covered label is the last resort, only
when caps forbid growth, and is reported.

**Insertion ("depth-first find a path").** Segments ordered
most-constrained first (shortest slack, highest fanout). Each is found by
search on the current geometry — the vector router's tangent graph (slice 3
`vroute`) or the CDT dual (slice 5 `topo`) — with obstacle inflation at a
fraction `α < 1` of `req` so a path exists where the full inflation would
wall it in; the resulting overlap is T3 stress; re-solving ERBS and then descending on
parts and vias is the shove (Reto: "insertion can shove other wires"). If descent cannot
zero the stress, the annealer tries SIDE_SWAP, ORDER_SWAP, VIA_INSERT, then
RIP_REINSERT on the 1-hop neighbours, depth-first with a bounded backtrack
budget per segment; a segment that exhausts the budget stays uninserted and
feeds T7 in the next round. Nothing already inserted is frozen — that is the
difference from the maze and from slice 3's post-pass.

**Schedule** (one run):

1. **Spread.** Variables: poses, labels. Terms: T1, T4, T5, T6, T8, T12,
   T14 (placement part), supply/ground tree lengths; λ_T4 ramped (augmented Lagrangian) until courtyard
   stress is 0; wells off; SA temperature high for ROTATE_JUMP/SWAP.
2. **Insert.** Segments inserted in order; after each, K descent steps on
   all variables (T2, T3 replace T1 for inserted nets; T7 for the rest). Any
   angle; T13 active. One pass is a round; rounds repeat while uninserted
   segments remain and the budget holds (the `anneal_route_loop` role). After
   topology selection, one joint spacing/compaction pass (ERBS).
3. **Harden.** β up, λ_T3 up, wells on (T9 coarse → fine, T10), T11's 45°
   preference ramped, temperature down. Then the exact snap: θ to its well,
   part origin to the finest active raster, segment angles to 45° multiples
   (the band → octilinear conversion, unless the board opts for any-angle output), labels to spots.
4. **Verify.** Kernel DRC (`drc.clearance_violations_naive` stays the
   independent oracle), `connectivity.net_islands`, label coverage, the bend
   check (T13), the return-path check (T15), a via-in-pad check on same-net
   pairs (gr496850; the kernel clearance pass exempts same-net), via layer
   span = through, THT holes unchanged. The pour runs **once**, then the
   existing pour connectivity DRC (e-pour-connectivity, 7ae9dfbe1) and the
   neck measurement (T17); a too-narrow neck becomes a force pushing the
   bounding tracks apart and the window loops back; an island counts as a
   failure like an unrouted segment. Violations the snap opens, and any
   residual, go back to step 2 for the offending window, with a budget; then
   the never-worse ratchet decides whether anything is written. Fewer steps
   than today's pour/re-check loop.

Where "depth-first" fits: step 2's insertion order and the bounded backtrack
over the discrete moves for a stuck segment. Where "gradient descent" fits:
every K-step descent, in all phases. Where "anneal" fits: the acceptance of
each move on the settled energy.

### Derivative route (per the `differentiation` skill)

Many variables into one scalar ⇒ reverse mode. Gradients are hand-written
analytic numpy/numba (the repo's existing stack); autodiff (JAX/PyTorch) is
not a dependency (aarch64 wheel risk, cf. dscribe; vector Decision 10 keeps
GPU out). Nonsmooth pieces are smoothed (`|·|` → `√(·² + ε²)`, max →
softplus or soft-min, snap → wells) where that is cheap; contact-change kinks
are handled by the trust-region step, not hidden. Frozen (discrete): group
membership, layer, via existence, homotopy class, band order. The kernel's
`gradient` is the one shared derivative of `sd`. Each term ships with a
finite-difference check test: directional dot-product, Taylor remainder
halving, central finite differences on a few variables at several steps
(`structsolve/simp.py` pattern), away from kinks. Finite differences are a
test, never the production gradient.

### Composition with the vector router slices

| Vector slice | Reused as | Order change |
|---|---|---|
| 1 `rules.required` | `req(a, b, L)` in T3, T8 | none; prerequisite |
| 2 clearance kernel | `sd`, `gradient`, STRtree pair culling for T3/T4/T5/T8; Verify | none; prerequisite. The kernel API must expose the derivative of `sd` w.r.t. each primitive's defining points (the "Better maths" row already names `gradient`) |
| 3 `vroute` tangent graph | the insertion search, with inflation `α·req`; funnel/band geometry | the post-pass stays as specced for the maze path; the unified path calls the same search as a library |
| 4 shared route cost | T2 layer multipliers on band edges | none |
| 5 CDT portals | insertion search alternative, the band funnel domain, and the SIDE_SWAP candidate generator (the sleeve on the other side of a portal) | none |
| 6a/6b topology assignment | the topology oracle only when insertion + backtrack fails on a benchmark; 6b (LP) stays optional | proposed: 6b waits for the unified path's benchmark numbers |
| 7 SCP cleanup (HiGHS QP) | only the SCP/HiGHS QP spacing cleanup is **superseded** (Reto 2026-10-18) by this spec's ERBS pass + descent; keep it only if it later measurably beats them on the bench. Slice 7's funnel string-pulling, order-preserving offsets and 45° conversion move into unified slice 4 | one spacing formulation wins: this one |
| 10 retire the maze | unchanged exit criteria | the unified path is a second candidate for the default beside `router='topo'` |

Slot: a new item consuming vector slices 1–2, plus a funnel domain (vector 5's CDT or vector 3's tangent graph) from slice 4 on, and vector 5 for slice 5's portal prices; landing
behind an opt-in `router='field'` until it becomes default (§Slices, slice
6). `optimize.OptimizeEngine` keeps its hard-locality rule: the descent is a
separate module (`src/precis/pcb/field.py`, new) called from the engine's
move loop, and the engine's per-move delta cost is not touched.

## Measures and exit criteria

Per board, every run, written by the vector slice 0 harness
(`scripts/pcb-route-bench`): segments inserted / total, kernel DRC errors and
warns by rule, labels placed / total, vias, track length, islands, wall time,
seconds per net.

Gates. Every slice is never-worse on every bench board (the existing
floors: nano mean ≥ 34.375, seed min ≥ 28, `silk_missing` ≤ 35, `clearance`
warns ≤ 9, ewod ≥ 51, qfp ≥ 53, and nano r2's floors in
`tests/test_pcb_nano_usb_pd_testboard_r2.py`, `BASELINE_DRC_ERRORS_BY_RULE`)
and under the never-worse ratchet. 100 %
routed and labelled is the thread exit only.

Exit (the thread's, not a slice's): on nano (8 seeds), r2, dogfood-b (named by
the coordinator's brief; r2 has a fixture, only dogfood-b lacks one — slice 0 of
the harness has to pin it as a snapshot), the ewod replay (55) and the
qfp-plaza member (55): **100 % segments routed, 0 DRC errors, 100 % labels
placed, no pour islands**, with runtime reported against vector Decision 8
(quality first; the job wall is the only hard cap).

## Slices

Each lands alone, with its test; smallest useful first. Each term's gradient
ships with its finite-difference check.

| # | Slice | Test |
|---|---|---|
| 0 | `field.py` skeleton: state packing, term interface `(value, grad)`, gradient-check harness (hand-written numpy/numba); T1 and T4 implemented | `tests/test_pcb_field.py`: dot-product, Taylor, FD on random boards for T1, T4; rotation gradient non-zero |
| 1 (remainder) | Placement polish is in (`field.polish_placement`, behind `OptimizeConfig.field_polish`, default off; SAT T4, T5, T6 landed). Evidence so far (flag on, 24 seeded boards of 6/8/12 parts): 2 applied, 15 illegal, 7 no_gain, so the legality gate refuses most. Left: measure with the flag on, then flip the default | nano 8 seeds and ewod not worse with `field_polish=True`; synthetic 3-part board converges to the known optimum |
| 2 | Wells T9 (anchor: part centre), T10 (k up to 24); θ becomes a real variable (ROTATE stops being cost-neutral) | synthetic board lands on 10 mm multiples and 0°/180° in preference order, flipped/rotated parts stay aligned; nano floors |
| 3 | Labels as variables (T8) with LABEL_JUMP fallback and the failure → OUTLINE_GROW (capped, only if it removes failures) → covered-last-resort order; `silk.build_silk` reads the stored label pose. No label pose exists in `PcbIR` or the store today: a new IR field (per-instance label position) is needed; if it needs a migration, that takes /go | `silk_missing` on nano ≤ 35 and reported; 100 % on a synthetic board the candidate ladder fails; first cases (gr495393): outlines still cut by a routed via — usbc-attiny-dogfood-b R3 at (24.07, 18.43) and J2 at (11.42, 18.21), and the nano (34) / nano r2 (47) cuts left once bulges are vetoed for costing a label |
| 4 | Bands: taut-band geometry (funnel on the CDT), T2 band length and tension, band order bookkeeping, T3 ordering + offset model, T13 bend rule, T11 ramp + band → octilinear conversion, ERBS spacing/compaction pass, kernel verify; vertex-descent fallback for the polish if needed. Absorbs vector slice 7's funnel, offset and 45° conversion. Needs vector slice 2 plus a funnel domain (vector 5 CDT or vector 3 tangent graph) | DRC warns not up, track length down on nano/ewod; tension gradient matches FD away from kinks; synthetic pinched corridor opens when a part is free; no turn sharper than 90° on any bench board |
| 5 | Shove by descent: insertion with `α·req`, joint trust-region descent on parts + bands + vias + labels; T7 with Lagrangian portal prices (needs vector slice 5; the chord fallback ships without it), optional LP lower bound; continuous vias (T5 hold-in, pad barrier) | synthetic two-wire corridor routes both; ewod ≥ 51 reported, target 55 |
| 6 | Discrete move set (SIDE_SWAP, ORDER_SWAP, VIA_JUMP, VIA_INSERT/REMOVE, LAYER_SWAP, RIP_REINSERT, capped OUTLINE_GROW/LAYER_ADD) + basin hopping + the full schedule, opt-in `router='field'`. Becomes default when it ties or beats today's router on all five bench boards; the old router is deleted then | the five boards' table; deletion lands in the default-flip commit |
| 7 | Placement: T12 arrays; T6 extended to (supply pin, ground pin) support pairs with PAIR_SWAP; T14 PDN term (IR + loop inductance, cross-side caps); array groups frozen from footprint + value + refdes run | ewod array spacing variance 0; nano decoupling loop area not up and caps within d₀; synthetic QFN puts an under-part cap on the opposite side only when much closer |
| 8 | Verify: pour once (tree in cost while routing, free re-merge after), grow-to-fill per net with IPC-2152 neck and via counts, T17 neck force, T15 return-path check and force, T16 bundling, island = failure | synthetic board: no islands, no neck below minimum, no fast signal over a slot; existing e-pour-connectivity DRC passes; fewer Verify loops than the old pour re-check |
| 9 | Exit run against §Measures; remove any remaining superseded path components the table names | the thread's exit criteria |

## Risks

| Risk | Mitigation |
|---|---|
| Piecewise-smooth kinks: gradients jump where a band gains or loses a contact; plain Adam stalls | trust-region step with Clarke subgradients and gradient sampling at detected contact changes; soft-min clearance; FD tests away from kinks |
| Band order bookkeeping: the cyclic order around obstacles, vias and Steiner points drifts out of sync with the geometry (a band slips past its neighbour) | order is annealer state, updated only by ORDER_SWAP/SIDE_SWAP/VIA_JUMP; a consistency check each step; mismatch rejects the step |
| Snap re-opening: the band → octilinear conversion and the rotation/raster snaps open violations | T11 ramp and T13 before the conversion; kernel verify after; residuals go back to Insert with a budget; the ratchet refuses a worse board |
| PDN term mis-weighting: α, β pull caps against courtyards, labels or routability; a wrong cross-side choice | start β small; the term only decides cross-side when much closer; per-board reporting of loop area; never-worse gate |
| Supply-domain clustering competes with signal-net pull (T1/T2): the supply tree length term drags parts together that signals want elsewhere | the tree weight sets the balance (supply > ground); tune on the bench boards; never-worse gate |
| Non-convex wells and penalties trap descent in a poor basin | wells only in Harden; basin hopping with ROTATE_JUMP/SWAP; `NANO_SEEDS` already measure multi-start |
| Gradient evaluation cost: ~10⁵ culled pairs × 10³–10⁴ steps; a funnel pass per moved band per step | numba pair kernels; pair list refreshed every K steps; measure in slice 0/1 before any schedule tuning |
| Smooth zero ≠ DRC zero (finite β) | kernel verify after the snap; the ratchet refuses a worse board |
| 100 % labels is infeasible on a dense board without growing it | OUTLINE_GROW as a capped discrete move (slice 3) |
| Descent cleanup duplicates vector slice 7 | slice 7 superseded here; revisit only if it measurably beats the descent |
| The hard-locality rule of `OptimizeEngine` vs a global descent | descent lives in `field.py`, called as one move; the engine's delta cost untouched |
| Hand-written gradients drift from the terms | the finite-difference check is a gate on every term |
| Rounds of insert/descend exceed the job wall on taz15-size boards | budget per segment and per round; the best round is restored (`anneal_route_loop` already does this) |
| Slices 0–3 land before the kernel exists and build a second distance code path | T4/T5/T8 use shapely polygon distance until slice 2 lands, then switch; the oracle test pins agreement |

## Decisions (Reto 2026-10-18)

Numbers 1–10 answer the former open questions; D11–D16 were approved after.
Folded into the body above.

1. Money is out of the objective; `pcb_cost` keeps reporting board_area, layer_count and via USD. OUTLINE_GROW and LAYER_ADD stay discrete, capped by `expansion_caps`, proposed only after a round ends with unrouted segments, dropped labels or pour failures, accepted only if they remove failures. A smooth outline-area term is parked.
2. Any-angle during descent; the 45° preference (T11) ramps in Harden; the band → octilinear conversion (exact 45° multiples) at the end and Verify repairs what it opens. Output default stays octilinear; any-angle output is a per-board option. Bend rule (T13): on octilinear output finished bends turn by 45° or 90° only; any-angle boards get turns ≤ 90°; no acute bends; penalty from the start, hard check after the conversion. No per-layer preferred direction.
3. Tracks are taut bands (shortest path per homotopy class, funnel on the CDT), not vertex variables. Band-length gradient is the contact tension (Danskin); explicit state is the cyclic band order per obstacle; band–band spacing is the ERBS pass (no gradient), whose reaction on parts and vias is the force. Vertex descent only as a polish fallback.
4. Label without a legal spot: T8 spreads parts, then LABEL_JUMP, then a failure that may trigger OUTLINE_GROW within caps; covered label only as the last resort when caps forbid growth, reported. Bottom-left is a well, not hard.
5. Rotation wells for every part, order 0 > 180 > 90 > 45 > 30 > 15, strong preference for multiples of 90; k=24 added with the smallest weight.
6. Raster anchor is the part centre via `snap_to_preferred`, not pad 1; the pitch set adds the part's own pitch from `_preferred_snap_grid_mm`.
7. One formulation: the descent here replaces the vector router's slice 7 SCP/HiGHS QP.
8. Gates: never-worse per slice on every bench board; 100 % routed/labelled only as the thread exit.
9. Hand-written numpy/numba analytic gradients, each with a finite-difference check test; no autodiff dependency (aarch64 wheel risk).
10. `router='field'` becomes default when it ties or beats today's router on all five bench boards; the old router is deleted at that point.
11. (D11) Mixed-integer nonsmooth bilevel framing (topology outer, taut-path value functions inner); `E` is piecewise smooth; trust-region with Clarke subgradients and gradient sampling; soft-min clearance; no global guarantee, hence the never-worse gate. Source `perplexity-research:496881`.
12. (D12) T7 uses Lagrangian prices on portals/corridors instead of per-round weight growth; fractional softmax over K candidates stays; optional multicommodity-flow LP lower bound; link to vector §Decisions 15–16 kept.
13. (D13) PDN term T14 (IR drop + loop inductance); support groups become (supply pin, ground pin) pairs with the ground pin fixed at start and PAIR_SWAP when stuck; caps may sit on the opposite side under the part; full PDN impedance is sign-off only.
14. (D14) Ground, every supply net and every pour net keep a tree in the cost while routing, excluded from T1 (supply trees cluster the parts of one supply domain; ground's weight is lowest since it spans every part); the pour runs once in Verify and re-merges freely; pour requirements (one piece, no islands, neck, vias, relief spokes, no slivers); return-path check T15; bundling T16; neck force T17; island = failure.
15. (D15) Per-net `grow_to_fill` (new per-net setting), default on for ground and nets > 0.5 A; neck width and via count from IPC-2152 width-for-current.
16. (D16) Vias are continuous beads on their bands, every via may move except footprint THT holes and features; obstacle crossing, VIA insert and remove are discrete jumps, each followed by a short settle; through-only with antipads; pads are hard via keep-outs.

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

From `perplexity-research:496881` (links unverified):

- RUPlace: Chen et al., DAC 2025, unified placement and global routing (ADMM + Wasserstein, bilevel)
- DGR, differentiable global router: Zhang et al., DAC 2024
- SURF extended rubber-band sketch (ERBS): Dai, Kong, Sato, "Routability of a Rubber-Band Sketch", DAC 1991
- TI guidance on 2-layer boards: power directly over ground, length:width ≤ 3:1
- NXP AN10897: solid ground plane, no splits

Ground and pour survey source: `perplexity-reasoning:496879`.

## Related

- [pcb-vector-router](pcb-vector-router.md) — kernel, `vroute`, `topo`; the slot above. Only its slice 7 SCP/HiGHS QP spacing cleanup is superseded, by this spec's ERBS pass + descent.
- [pcb-global-plan-probe](pcb-global-plan-probe.md) — why hard corridors lose.
- `optimize.anneal_route_loop` (the shipped pcb-anneal-closes-router-loop item) — the loop this replaces.
- `docs/backlog/threads/pcb-platform.md` Horizon.
