---
status: draft
title: The pre-routing routability estimate cannot see net-class layer restrictions, so it reports a trivially-routable board that then fails 40 of 55 nets
prio: high
pillar: 3d-design
---

# "planar ✓, 0 vias needed" on a board that routes 15 of 55 nets

Dogfooded on prod, `ewod-dogfood-6`, 2026-10-01. The three views an agent
would consult *before* spending a route, on a board whose actual route
realized 15 nets and failed 40:

```
# route feasibility (estimate, not real routing)
airwires: 55  (H 55 / V 0)
residual same-layer crossings: 0
≈ vias needed: 0

# crossings — 0 (the pre-routing objective; plane nets excluded). ratsnest 777.817 mm
(no crossings — planar so far ✓)

# congestion — last route: 15 realized, 40 failed, 0 gap warning(s)
(no over-capacity gaps ✓)
```

Every one of those is green. Two carry a tick. The board fails 73% of its
nets.

## Motivation / why

This matters more than a cosmetic mismatch because of the architecture it
sits in: the whole point of the layered IR is that "the optimizer can work
as deep in cheap graph space as it can and descend only when forced"
(`precis.pcb.ir` module docstring). The cheap-space estimate is the thing
that decides whether a placement is worth routing. Here it is structurally
incapable of seeing the constraint that actually dominates the board.

`place.route_feasibility` is purely geometric:

```python
(h if abs(w.p1[0] - w.p2[0]) >= abs(w.p1[1] - w.p2[1]) else v).append(w)
residual = len(ratsnest.crossings(h)) + len(ratsnest.crossings(v))
return {..., "vias_estimate": residual}
```

It assigns each airwire to a notional horizontal or vertical layer by
dominant direction, counts residual same-layer crossings, and uses that
count **as** the via estimate. So a via is only ever predicted as the
remedy for a crossing.

**On this board every escape needs a via for a reason crossings cannot
express.** The `ewod_ARR1_escape` net class is `{"layers": ["B.Cu"]}` while
the electrode pads it starts from are on F.Cu. Each of the 55 escapes is
therefore forced across a layer boundary by its own net class, independent
of whether anything crosses. The true lower bound is 55 vias; the estimate
says 0, and it cannot say otherwise because net classes are not among its
inputs — it takes only a list of airwires.

**The docstring is honest and that is not the problem.** It says "NOT real
routing", "coarse", and "the rented router is authoritative". The gap is
that the estimate omits a constraint that is *cheaply computable at the same
level of abstraction it already works at* — no geometry, no routing, just
"does this net's class permit the layer its pads sit on". An estimate that
is coarse is useful; one that reports zero where the floor is 55 is not
coarse, it is silent about the governing constraint.

The `congestion` view has the milder version of the same shape: it reports
gap capacity, which is a legitimate thing to report, but it prints
`(no over-capacity gaps ✓)` directly beneath `40 failed`. An agent reading
it to answer "why did routing fail" is told there is no problem here.

## In scope

- `route_feasibility` gains a layer-restriction term: for each net, if the
  net class's permitted layers exclude the layer its pads sit on, that net
  contributes at least one via to the estimate. Report it as a floor
  (`≥ N vias`) rather than an approximation, because unlike the crossings
  term it is a hard lower bound, not a guess.
- Its inputs widen from `list[Airwire]` to also carry per-net rules — the
  machinery exists (`precis.pcb.rules`, and `realize._side_layer` already
  answers which layer an instance's pads are on).
- The `congestion` view's green tick is suppressed, or qualified, when the
  last route failed nets. "No over-capacity gaps" with 40 failures should
  not read as a pass.

## Explicitly NOT in scope

- Making the estimate accurate. It is a coarse estimate by design and
  should stay one; this item is about a constraint it is blind to, not
  about closing the gap to the real router.
- Explaining *why* those 40 nets failed. That is
  `pcb-placer-starves-the-escape-corridor.md` and the placer's
  fixed-copper blindness
  (`pcb-placer-obstacle-set-is-mounting-holes-only.md`); this item is only
  about the estimate claiming the board is fine beforehand.
- `implied_via_count`'s `PAD_LAYER` hardcode — related in spirit (a via
  prediction that assumes pads are on F.Cu) and already tracked as defect A
  in `pcb-via-geometry-ignores-pad-side-and-pads.md`. Do not fix both in one
  change; they are different functions with different callers.

## Acceptance criteria

- On a fixture whose net class forbids its pads' layer, `view='feasibility'`
  reports a via floor of at least one per such net. The negative control
  matters: a board with no layer-restricted classes must still report the
  crossings-based number unchanged, or the term is just added everywhere.
- `view='congestion'` does not print a passing tick when the stored route
  has failed nets.
- A test asserting the floor is a FLOOR — construct a board needing more
  vias than the restriction implies and assert the estimate does not exceed
  the real count, so the term cannot be "fixed" by inflating it.

## Target + blast radius

`src/precis/pcb/place.py::route_feasibility` (and its signature, so every
caller), `src/precis/handlers/pcb.py` around the `feasibility` and
`congestion` view renderers (line ~1049 composes the `≈ vias needed` line).
Changes a reported number on every board, so any test pinning the current
estimate moves — re-measure rather than assuming direction.

## Open questions / decisions log

- Should `feasibility` refuse to render a tick at all, or is the fix purely
  numeric? Leaning: the number is the fix, and the tick belongs to
  `crossings`/`congestion`, which should learn about failed nets.
