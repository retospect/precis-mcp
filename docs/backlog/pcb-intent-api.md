---
status: draft
title: pcb intent API for workspace code — objectives, constraints, regions, geometry refs
pillar: 3d-design
prio: high
model: fable
blocked-by: pcb-workspace-code
---

# pcb intent API for workspace code

Reto 2026-10-24: what preferences should code (ultimately the LLM) give
the placer and router? His examples: a placement objective "minimize
ratsnest 80% / shortest wires 20%"; a router rule "<area> L3: horizontal
only"; areas as coordinates *or* object-relative ("IC1 outline", "R4
centrepoint"). This item is the script-level surface for that intent,
on top of [pcb-workspace-code](pcb-workspace-code.md) (`pcb.Board`
builder, `pcb.place`/`pcb.route`) and gr513867's direction (Reto: "router
should just be part of the library and another command in the code" —
`route(board, ...)` runs in-script, DB-free, returns a result object and
is a traced frame).

## Who sets weights (Reto 2026-10-24, verbatim)

> "sure it does, the LLM's main job is to tweak these to best practices."

The LLM, as designer, sets and adjusts objective weights and constraints
in code, following best practice, with the reason written next to each
call. The **numeric optimiser** (anneal, descent, field polish, BO) never
changes the weights of the objective it is minimising within a run —
that would be self-grading. The outer LLM loop tunes weights explicitly
*between* runs; every run records the weights it ran with. The house
guard in [multiscale-design-architecture](multiscale-design-architecture.md)
§Optimisation stack ("a solver may not tune its own objective") is
clarified in the same commit: "solver" means the numeric optimiser. This
feeds gr513747: the optimisation toolkit takes weights as designer-set
inputs and records them per trial; its BO/anneal chooses *where to
evaluate*, never the weights.

## Weights over normalised terms (reconciles method §3)

[multiscale-optimisation-method](multiscale-optimisation-method.md) §3
rules out raw weights across units ("a weight is secretly a unit
conversion factor") and decides **fixed reference values R_i** measured
once from a starting design. The intent API keeps that rule and puts the
designer's preference *after* normalisation:

    J = Σ_i  w_i · f_i(x) / R_i        R_i = f_i(x_ref), fixed for the run

- `x_ref` is the seed placement (or a named earlier run, `ref=run`).
  R_i is measured once, held constant (adaptive normalisation stays
  rejected), and recorded on the run.
- `w_i` are **shares at the reference**: weights are normalised to sum
  to 1, so at `x_ref` term i contributes exactly `w_i` of J. "ratsnest
  80 % / wires 20 %" means literally that. The same `w` on two boards or
  two code versions means the same priority — weights are comparable.
- A scalarised sum, not a Pareto front. Pareto preference is the later
  option: the same `w` as Chebyshev weights over the same normalised
  terms (method's "fit per objective, then Chebyshev-scalarise"), so
  switching mode does not change what a weight means.
- `objective=None` is the house objective: `cost.py`'s USD sum as today
  (already one currency — `board_area_usd_per_mm2`, `via_usd`,
  `risk_to_money`, `routing_area` priced as area). Bit-for-bit today's
  anneal; the long-run single-currency target is untouched.
- A term with `R_i ≈ 0` at the reference (no crossings at the seed)
  falls back to a declared floor per term (one per term, in code, not a
  knob) and the echo says so.

## Hard constraints vs soft preferences

Every constraint call takes `hard=True|False`; there is no implicit
mode. **Hard** enters legality (`OptimizeEngine._placement_is_legal`,
the router's edge filter) and is never traded; an unsatisfiable hard
set refuses with the conflicting pair named. **Soft** is a penalty term
added to J with its own `w` (same share semantics). Defaults: keepouts,
rooms, clearance, width, `fixed` are hard; directions, `near`/`apart`,
length targets soft.

## The surface

```python
obj = pcb.objective(
    ratsnest=0.8,              # crossings + ratsnest length (cost.py terms)
    wirelength=0.2,            # routing_area
    # also: vias, area (outline bbox), layers, measures, plane_affinity
    ref="seed",                # or ref=<run> for cross-run comparability
)
pcb.place(b, objective=obj, seed=1, iters=2000, budget=pcb.budget(seconds=300))
pcb.route(b, placement=pl, objective=pcb.objective(vias=0.3, wirelength=0.7),
          loop=2, budget=pcb.budget(max_expansions=60_000, vroute_expansions=6_000))
```

| intent | call |
|---|---|
| region | `pcb.region(expr)` — a named, lazily resolved polygon |
| room (placement) | `b.room(name, region, parts=[...] \| selector=, side='top', hard=True)` |
| keepout | `b.keepout(region, layers=[...], what='parts'\|'copper'\|'vias'\|'all', hard=True, reason=)` |
| direction rule | `b.route_rule(region, layer='In2.Cu', direction='h'\|'v'\|'d'\|<deg>, hard=False, w=)` |
| layer allow/deny | `b.route_rule(region, nets=cls_or_nets, layers=[...], hard=True)` |
| net class | `b.net_class(name, width_mm=, clearance_mm=, pair_rules=, priority=)` (exists, plus `priority`) |
| net priority | `net.priority(1..10)` — route order and rip-up protection |
| diff pair | `b.diff_pair(p, n, gap_mm=, width_mm=, max_skew_mm=)` |
| length match | `b.length_group(nets, tol_mm=, target_mm=None, hard=False)` |
| near/apart/align | `b.near`, `b.apart`, `b.align`, `b.measure(..., hard=, w=)` (exist; `weight=` → `w=` share) |

**Geometry references** are lazy expressions resolved at place/route
time against the poses the solver is evaluating:

```python
u1.outline        u1.courtyard        r4.center        u1["VCC"].center
pcb.box(x0, y0, x1, y1)   pcb.circle(c, r)   pcb.poly([...])   b.outline
expr.buffer(mm)   expr.offset(dx, dy)   a | b   a & b   a - b   expr.hull()
pcb.between(u1["SW"].center, l15.center).buffer(1.0)    # a corridor
```

Resolution rules:
- A ref to a **fixed** part (or coordinates) resolves once per run.
- A ref to a **movable** part resolves per evaluation for soft terms and
  at each anneal-stage boundary for hard placement constraints (a room
  that moves every move makes legality non-monotone). Router rules
  resolve once, after placement, when every pose is known.
- An expression that cannot resolve (unknown refdes, empty
  intersection) fails `b.check()` offline, with the script line.
- The resolved polygon is recorded per stage on the run (below) so a
  digest can draw what the rule meant, not only what was written.

## Recording and surfacing

- **Manifest.** `out/manifest.json` gains `intent: {objective, constraints,
  rules, budgets}` in canonical form (exprs as ASTs, not polygons). It
  is part of the request key, so a changed weight re-runs and an
  unchanged intent hits the cache (pcb-workspace-code §Requests).
- **Trace.** Each `b.room`/`route_rule`/`objective` call is a traced
  frame like any builder call. The `place(...)`/`route(...)` service
  frame's `args` carry `intent_sha`, the R_i vector, the resolved
  region polygons and the final per-term values f_i, f_i/R_i, w_i·f_i/R_i.
- **Persisted with the run.** The same block lands in job meta and
  `last_route`/placement run rows; `view='trace', args={'against': run}`
  diffs intent between runs and code versions ("w.ratsnest 0.8 → 0.6,
  room PWR buffered 1 → 2 mm; crossings 14 → 9, vias 40 → 52").
- **Digest.** `Placement.summary()`/`Routing.summary()` and
  `view='route-status'` name the **binding** constraints: hard ones at
  their bound (a room edge holding U_B15, a keepout that blocked the
  last free channel for CC1, with `marginal price` from the router's
  conflict digest where it exists), soft terms ranked by share of final
  J, and unmet rules ("route_rule In2 'h' violated by 3 segments, 4.1 mm").
  Each line carries the frame of the call that declared it, so the LLM
  knows which line to edit.

## Worked example — nano (`examples/boards/nano_usb_pd/main.py`)

```python
# Three bucks share a 4-layer board; switch nodes must stay tight.
pwr = (u_b15.courtyard | l15.courtyard | c_in15.courtyard).hull().buffer(1.5)
b.room("BUCK15", pwr, parts=[u_b15, l15, c_in15, c_o15], hard=True)
# SW15 loop: no foreign copper under the inductor on the top layers (TI SLVA958).
b.keepout(l15.courtyard, layers=["F.Cu", "In1.Cu"], what="copper",
          hard=True, reason="SW node coupling under L15")
# USB-C CC lines stay short and over GND; In2 carries power regions vertically.
b.route_rule(b.outline, layer="In2.Cu", direction="v", hard=False, w=0.1)
b.route_rule(j_usb.courtyard.buffer(4), nets=[cc1, cc2], layers=["F.Cu"], hard=True)
cc1.priority(2); cc2.priority(2)              # route before the GPIO fan-out
b.net_class("power", width_mm=0.6, clearance_mm=0.2, priority=3)

pl = pcb.place(b, objective=pcb.objective(ratsnest=0.8, wirelength=0.2), seed=1)
rt = pcb.route(b, placement=pl, objective=pcb.objective(vias=0.4, wirelength=0.6), loop=2)
print(rt.summary())
# 58/64 routed · binding: keepout L15/F.Cu blocks SW15→C_O15 short path (frame main.py:318)
# soft: wirelength 0.52, vias 0.31, In2 'v' 0.17 (3 segs off-axis)
```

## Mapping — intent → existing mechanism or GAP

| intent | exists today | status |
|---|---|---|
| objective terms | `cost.py` USD terms (`crossings`, `routing_area`, `via_usd`, bbox area), `CostConfig` | weights-as-shares + R_i: **GAP** (small; wraps `CostConfig`) |
| place budgets/seed | `pcb_place` params `iters`, `seed`, `snap`, `groups`, `field_polish` | exists |
| route budgets | `RealizeConfig.max_expansions` (60k), `vroute` 6k, `loop` ≤ `LOOP_MAX_ROUNDS`=3, `expansion_caps`, `budget_s` | exists; `pcb.budget` is a rename |
| in-script route | job-only; `bench.py` knows sanity/power; no DB-free Board→IR | **GAP** (gr513867) |
| fixed / leash / side | component `fixed`, `max_travel_mm`, `fixed_side`, `group_fixed` (0219) | exists |
| rooms | `pcb_instances.room_id` column, handler validates `ftype='room'` | enforcement **GAP** (stackup-regions slice 5) |
| keepouts | `features[keepout]` stored | binding **GAP** ([pcb-keepout-does-not-bind](pcb-keepout-does-not-bind.md)) |
| layer direction per region | stackup `direction` key validated (`ir.STACKUP_DIRECTIONS`); maze/globalplan removed; `vroute` has no direction cost; "no per-layer preferred direction" (unified place-route Decision 2, 2026-10-18) | **GAP + ruling needed** |
| layer allow/deny per net | none (`locked_plane_nets` covers planes only) | **GAP** |
| width / clearance / pairwise | `net_classes` rules, `pair_rules`, `resolve_net_rules` | exists; per-region override **GAP** |
| net priority | `realize._spread_order` (derived order) | **GAP** (author override) |
| diff pairs, length match | `objectives.matched_length` weight only; constraint-classes §E-1: "neither exists in the engine" | **GAP** (large) |
| measures near/apart/align | `MeasureSpec`, only pair measures steer placement | partial |
| pin/plane affinity | `plane_affinity` term specced | **GAP** (stackup-regions slice 6) |
| lazy geometry refs | none; features are literal polygons | **GAP** |
| intent on the run | trace frames specced (pcb-workspace-code §Construction traces) | **GAP** (intent block, R_i, digest of binding constraints) |
| pr engine | `precis.pcb.pr` v1 (free parts, beliefs), not wired; vroute stays production | consumes the same intent block once it beats vroute |

## Slices

| # | slice | size | tier |
|---|---|---|---|
| 1 | `pcb.objective` + R_i + shares → `CostConfig` scaling; `objective=None` bit-for-bit; R_i and per-term table on the run | S | Sonnet/Devin |
| 2 | geometry ref expressions (`outline`, `courtyard`, `center`, pin centre, boolean/buffer/offset) resolved by shapely against an IR pose snapshot; `b.check()` validation | M | Sonnet |
| 3 | intent block in manifest + request key + service-frame args + `view='trace' against` diff | S | Sonnet |
| 4 | binding-constraint digest on `summary()`/`route-status` (hard-at-bound, soft shares, unmet rules, frame column) | M | Opus (needs judgement on "binding") |
| 5 | rooms + keepouts hard in placement (= stackup-regions slice 5, keepout-does-not-bind) taking ref regions | M | Sonnet |
| 6 | router rules: per-region layer allow/deny (hard edge filter in `vroute`), direction as soft edge cost | M | Opus (vroute cost model) |
| 7 | net priority → realize order + rip-up protection in `anneal_route_loop` | S | Sonnet |
| 8 | in-script DB-free `pcb.route` (Board → batch → IR without `pcb_apply`), gr513867 | M | Opus |
| 9 | diff pairs + length groups (coupled routing, skew DRC, serpentine) | L | Opus; own spec |

1–3 can land in one wave; 4 needs 1–3; 6 and 9 wait on the rulings below.

## Questions for Reto

1. **Direction rules vs Decision 2 (2026-10-18).** You ruled out
   per-layer preferred direction; your example is "L3 horizontal only".
   Is that hard (filter edges off-axis in the region) or soft (cost)?
   Recommend soft by default, `hard=True` allowed per region.
2. **Shares or raw weights.** Recommend shares at the reference (80/20
   means 80/20 at the seed). Alternative: weights multiply the house USD
   prices (w=1 = house). Shares make weights comparable across boards;
   USD keeps the single-currency path. Which?
3. **Placement objective "ratsnest".** Read as crossings + ratsnest
   length, with "shortest wires" = `routing_area`. Correct?
4. **Movable-part refs in hard placement constraints** — allow (re-resolved
   at stage boundaries) or refuse (only fixed parts/coordinates)?
   Recommend allow, stage-boundary resolution.
5. Diff pairs / length match: in scope for the nano/EWOD boards now, or
   park as slice 9 until a board needs them?
