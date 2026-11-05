# topo-route-legalise — joint legalisation of the topo embedding (td569542)

Reto's model: route completely in math space (zero-width wires, no
crossovers), then shove things slightly — vias, wire offsets, unfrozen
parts — to make it 100% realisable, then optimise. The embedding exists;
no stage grows it into legal copper by moving anything. This slice is
the missing stage, a legalisation round after the wire search and before
emission, extending `shove.py::shove_pose`'s LP idiom.

## Variables (per round, LP)

- **Via centres** — 2-D per `wires.PlantedVia`, bounded to the hop
  edge's triangle pair (`mesh.edge_tris[e]`) so the homotopy class
  holds, ±`_CAP_MM` trust region.
- **Wire offsets** — 1-D lateral offset per wire per crossed edge on a
  contested `(edge, layer)` cut: the crossing's along-edge position,
  bounded to the edge's free stretch for that layer (pad covers at the
  finest rule, minus via covers), ±`_CAP_MM`. `_spoke_offsets`' shared-
  vertex fan is the degenerate case.
- **Part translations** — 2-D per rigid unit, unfrozen only, respecting
  `fixed`, rigid groups, hard measures (measure endpoints are excluded —
  `proxy::settle_measures` owns them), ±`_CAP_MM` per round, legal box,
  containment hold, triangle signed-area guard.

## Rows

- Cut rows per contested `(edge, layer)`: ordered adjacent spacing
  `(w_i+w_j)/2 + max(c_i,c_j) + FAB_QUANTUM_MM` plus end margins
  `w/2 + c − (t_ref/2 + c_ref)`, soft with a high slack price; interval
  ends linearised against the bounding cover's owner (part translation
  or via move projects onto the edge direction).
- Via keepouts linearised at the current point: pad bodies
  (`_ViaPlanter.pad_ix`, own-pad mask-web rule), env bodies (zones,
  outline), drill holes, other planted vias and earlier wires —
  `dist ≥ r_site + r_b + max(c_q, c_b) + FAB_QUANTUM_MM` half-planes,
  soft with the same high slack price. Pad/via obstacles owned by LP
  variables carry their motion into the row.
- Part rows: same-side courtyard MTV separation and zone intrusions
  (soft, `_SLACK_W`), triangle signed-area guard (hard), legal box and
  |δ| ≤ `_CAP_MM` (hard), epigraph |Δ| rows for every variable (hard).

## Loop

≤4 rounds (default): build rows at the current point → solve (HiGHS,
the solver `shove_pose` already uses; objective min Σ|Δ| L1) → apply
(parts through the containment hold + `build_mesh` gate with bisection;
vias clamped into the triangle pair and re-certified with
`_ViaPlanter.plantable_points`; offsets clamped into the free stretch)
→ re-derive. Stop when nothing moved or the LP is infeasible. Rows still
violated after application name their nets — handed back to the search
as nets to re-route last (the plan's existing `no_via`/`guarded` path).

## Emission and worker

`legalise_route` runs inside `realise::_route_pass` after `build_wires`
and before `_spoke_offsets`/`plan_route`: moved via centres rewrite
`PlantedVia.x/y` (and the wire vertex) — homotopy preserved. Crossing
offsets are NOT baked into `wires`: `b.t`/`b.edges` stay the search's
corridor record and the placements go to `_spoke_offsets` as forced
`placed` crossings, which owns turning them into polyline vertices
under its own guards (sharp-turn drop, via-window clip, pad-sampled
free stretch). When a placement produces short-risk copper on the
EMITTED geometry the offending nets' placements are dropped and
emission retried. Part translations are returned on a sink;
`pcb_place::_topo_route` and `route_design`'s own recovery consume
them as candidate poses inside the keep-best-by-failed-count loop.

## Corridor overflow as a legalise input (cleanup-66)

A `CorridorOverflow` raised by `_route_pass` is not an immediate
refusal: `realise::route_design` collects the legalise shove poses the
overflow round produced and retries `_route_design_once` at each,
bounded. A retry counts only when it routes EVERY net
(`_plan_failed_nets` empty) — a pose that clears the cuts but still
fails nets leaves the original overflow standing, so the failure
class stays `LocalError`/`CorridorOverflow` exactly as before. The
pose the returned plan was built at is reported on `pose_sink`;
`local._route_stage` stamps it into `ir` (gr541685) and into the
returned pose maps.

Inside `legalise_route`, each non-refused `CorridorCut` adds a soft
capacity row: the edge's binding layer is the one with the least TOTAL
uncovered length (the corridor's own `_pad_eval` measure, fully
covered layers included). The deficit `(demand − cap) · slot_mm`
(+ `FAB_QUANTUM_MM`) becomes required free-length gain, linearised
against the covers bounding the widest free stretch — part
translations move the bounds apart along the edge direction; a
fully covered layer frees an edge end by retreating the outermost
cover's owner away from the cut's other named part. Edges whose free
stretch is bounded by the edge ends on both sides, or whose covers
have no movable owner, emit no row — the cut then stays a refusal.

Acceptance: `examples/boards/usbc_attiny` still raises — GND demand
outgrows what part shoves alone can supply (cuts regenerate at new
edges as the search re-routes); the strict-xfail row in
`tests/test_pcb_examples_route.py` stays until the board routes.

## Non-goals

No change to the wire search (`build_wires`, `_net_tree`, `_Occupancy`,
net order), no GND-as-tree, no H/V layer rule, via class unchanged.
