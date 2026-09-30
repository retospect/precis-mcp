---
status: draft
title: Layers have no preferred routing direction, and the angle could be annealed rather than declared
prio: normal
pillar: 3d-design
---

# Preferred direction per layer, annealed at ratsnest time

## Motivation / why

Every routable layer in this engine is directionally isotropic: nothing in
`realize.py`'s maze or `optimize.py`'s cost function prefers a horizontal
trace on one layer and a vertical one on the next. That convention —
In2.Cu horizontal, In1.Cu vertical, or any consistent alternation — is how
real multilayer boards keep a dense fan from self-blocking, because two
layers routing the same direction contend for the same channels while two
layers routing orthogonally do not.

This surfaced on the EWOD dogfood (2026-09-29) when all four layers were
opened for routing. Opening layers adds capacity; it does not by itself
stop the new capacity being spent in the same direction as the old, which
is the case where a second signal layer buys much less than its area
suggests.

**The idea worth keeping is the second half: anneal the angles, do not
declare them.** A fixed H/V convention is the textbook answer and is easy
to add, but the preferred angle per layer is a free parameter the
optimizer could settle the same way it settles placement and layer
assignment — chosen at ratsnest time from the actual connection geometry
rather than fixed at 0/90 by convention. A board whose escape fan runs
radially out of a pad array has no reason to prefer 0 and 90 over 30 and
120. Consider a per-layer preferred angle as a real optimizer variable,
with the classic H/V as the special case the anneal should find on a board
that wants it.

## In scope

1. **A preferred-direction term.** Per routable layer, a preferred angle
   plus a cost penalty for segments deviating from it. The term has to
   read actual segment geometry, which distinguishes it from `crossings`
   (component-centroid granularity, structurally blind to angle — see the
   `SIDE_FLIP`/`ROTATE` inertness note in `precis-pcb-route-help`).
2. **Anneal the angle itself.** Add the per-layer preferred angle to the
   optimizer's state so a move class can perturb it, rather than taking
   it as authored input. Authored override stays available.
3. **Report the settled angles** so a board's layer directions are
   inspectable after a route, not implicit.

## Explicitly NOT in scope

- Hard-constraining a layer to one direction. Real boards break the
  convention locally; this is a cost term, not a rule.
- The escape crossing / pin-swap objective (item 3 of
  `pcb-escape-and-driver-chain.md`). Related in that both aim at the same
  congestion, independent as levers.

## Open questions / decisions log

- **OPEN — is the angle per layer, or per layer per region?** A board with
  two differently-oriented dense areas may want different angles in each.
  Starting per-layer is simpler and matches the convention being imitated.
- **OPEN — does the current cost function have any term that can see a
  segment angle at all?** If not, this item carries the same lifting cost
  as `pcb-placer-starves-the-escape-corridor`: the measurement has to move
  to where the optimizer can reach it before the term can exist.
- **OPEN — how is this evaluated?** Escape yield is a poor criterion (see
  the placer item). A direction term should be judged on channel
  contention directly, which needs the pinned crossing metric item 3 now
  blocks on.
