---
status: draft
title: The placer prices every edge of a net alike, so "this pin's bypass cap" and "a connector on the same rail" are the same constraint
prio: normal
pillar: 3d-design
---

# The placer cannot express "this is the part that must be closest"

Reto, 2026-09-30, while ruling on
`pcb-always-valid-board-invariant.md`: **"it occurs to me that placer may
want to know tightest connected part (ie bypass cap, next thing on the bus
etc)"**.

## Motivation / why

`cost.py` prices placement through ratsnest length over each net's members,
and a net is a hypergraph edge with no internal structure. On a real board
that flattens distinctions that dominate the layout:

- A bypass cap must sit within roughly a millimetre of the pin it decouples,
  because the loop area *is* the function. On `GND` and `VDD_LOGIC` it is
  one of dozens of members, and its edge is priced identically to a
  board-edge connector's.
- A bus has an *order*. "The next device along this bus" is an asymmetric,
  ordered relationship between two instances; net membership is a set.
- A sense line's partner is the thing it must be short to, not the whole
  net.

So the one relationship that most constrains a good placement is exactly
the one the objective cannot see, and today the only way to get it is to
pin parts by hand — which contradicts Reto's 2026-09-29 placement ruling
("placement should always be done with the placer ... most of it should
move").

**This is a graded PREFERENCE, not a legality rule.** Per the same session's
canonical ruling, legality is a hard gate and preferences are graded, so
this belongs in the objective. But note the trap it walks into: `risk()` is
a criticality-weighted MAX over `Family.MARGIN`, so a new graded term that
lands in the wrong family contributes exactly zero unless it is the maximum
— see `pcb-risk-is-a-max-so-any-money-term-is-a-free-tiebreaker.md` before
choosing where this term lives.

## In scope

- A per-pin or per-instance-pair **affinity** fact: "these two pins want to
  be within N mm", with a strength, distinct from net membership.
- A cost term that reads it, priced so a violated tight affinity outranks
  ordinary ratsnest length rather than averaging into it.
- Where the fact comes from: authored at netlist-binding time (the LLM
  wiring a decoupling cap already knows it is a decoupling cap — the same
  "capture it at binding, it is expensive to reconstruct later" argument
  `pcb-component-model.md` makes for pin roles), with the *decoupling*
  special case derivable automatically, since a two-pin passive between a
  power pin and ground next to an IC is recognisable from the netlist alone.

## Explicitly NOT in scope

- Bus ordering as a routing constraint (layer/topology) — this item is
  placement distance only.
- Replacing `measures` (`put(args={'measures':[...]})`), which already
  prices an author-stated proximity goal between two named INSTANCES. That
  is the closest existing mechanism and may be the right substrate: this
  item may reduce to "derive measures automatically from the netlist and
  make them per-PIN". Decide that before building a parallel system.
- Inter-instance equivalence (two 100 nF caps on a rail interchangeable
  with each other) — `pcb-component-model.md` owns that, computes it from
  the netlist with no LLM, and it is a different fact.

## Acceptance criteria

- A bypass cap authored with a tight affinity to a specific IC power pin
  ends up adjacent to that pin on a board where the unconstrained placer
  puts it elsewhere, and the test asserts the DISTANCE, not merely that the
  term is nonzero.
- Negative control: a part with no affinity is not dragged anywhere — the
  term must be inert when unstated, so an imported netlist without
  annotations degrades to today's behaviour rather than guessing.
- The term's family and aggregation are stated explicitly in the code, with
  the `risk()`-is-a-MAX interaction named, or the term can be silently
  zero.

## Target + blast radius

`src/precis/pcb/cost.py` (the new term), `src/precis/pcb/optimize.py` (if
the annealer needs a new move type to exploit it), the netlist-binding
surface in `src/precis/handlers/pcb.py`, and `precis.pcb.measures` if the
decision is to generalise measures rather than add a term. Moves placement
on every board that carries an affinity, so it changes existing placement
numbers — re-measure rather than assuming a direction.

## Open questions / decisions log

- Generalise `measures` to per-pin, or add a separate affinity relation?
  Leaning generalise: it already exists, already has a cost curve, and
  already prices hard-vs-soft decisively rather than as a legality
  rejection.
- Is the decoupling case worth auto-deriving in v1, or does authored-only
  ship first? Auto-derivation is a netlist pattern match with no LLM, so it
  is cheap — but it is also the case where being wrong is most visible.
