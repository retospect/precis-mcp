---
status: draft
title: risk() is a MAX over margins, so any money term is a free tie-breaker against every non-maximal constraint
prio: normal
---

# risk() is a MAX over margins, so any money term is a free tie-breaker

## The finding

`precis.pcb.cost` splits terms into two families. `Family.MONEY` terms sum
into dollars. `Family.MARGIN` terms are fractions-of-budget, and `risk()`
combines them with a **maximum** (a criticality-weighted hardened max), not
a sum.

The consequence is structural, not a tuning problem: **a margin term that
is not currently the maximum contributes exactly zero to the objective.**
Moving it — improving it, worsening it — changes nothing until it becomes
the max. So every non-maximal graded constraint is invisible at the margin,
and any MONEY term, however small, becomes a free tie-breaker that decides
among all of them.

## The evidence that forced this to be written down

Adding `routing_area` (a MONEY term, area swept by a strand priced at the
same $/mm² the bounding box pays) on 2026-09-30:

- On the nano fixture it is worth **$0.046 against a $5.21 total** — under
  1% of money.
- It nonetheless **won two separate fights** against graded margin terms:
  it held `ARR1_SINK_0` under the array (which the side-aware placer fix had
  made movable, and which nothing else priced), and it outweighed
  `courtyard_overlap` hard enough that `U1` stayed sitting on a mounting
  hole until `seed_placement` was changed to avoid holes outright.

A term worth 0.9% of the objective should not be able to overrule a
constraint. It can because the constraint was not the max.

This is **not fixable by shrinking the new term's weight** — any positive
weight beats zero. That is the point of recording it.

## Why it is not obviously a bug

The MAX is deliberate for its original purpose: risk is "how close is the
worst thing to its budget", and summing near-misses would let many
comfortable margins mask one violation. The defect is the interaction with
money, not the max itself.

## Options, none chosen

1. **Soft-max with a floor contribution** — every margin contributes a
   small monotone amount so improving a non-maximal one is never free-zero.
   Changes every existing cost number; needs a re-baseline.
2. **Lexicographic** — money only breaks ties among placements whose margin
   vector is equal, rather than trading against it. Closest to the stated
   intent; expensive in the annealer's inner loop.
3. **Accept and document** — treat money terms as deliberate tie-breakers
   and require that any new money term be justified as one. This is the de
   facto status quo; `routing_area` shipped under it.

## In scope

Pick one. If (3), say so explicitly in the `cost` module docstring so the
next person adding a money term knows the leverage they are getting.

## Explicitly NOT in scope

Removing `routing_area`. It is doing useful work; this item is about the
mechanism that made it *disproportionately* effective.

## Acceptance

A test that pins the chosen semantics: construct two states differing only
in a non-maximal margin term and assert the objective does (option 1/2) or
does not (option 3) distinguish them.

Owner anchor: `precis.pcb.cost` — `risk()` and the `TermSpec` registry.
