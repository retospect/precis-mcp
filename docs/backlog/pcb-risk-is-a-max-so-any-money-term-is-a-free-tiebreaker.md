---
status: draft
title: risk() is a MAX over margins, so any money term is a free tie-breaker against every non-maximal constraint
prio: normal
pillar: 3d-design
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

## Reto's ruling, 2026-09-30 — legality leaves the objective entirely

Asked whether this decides "can item 3's fix be graded, or must it be a hard
gate", Reto made `pcb-always-valid-board-invariant.md` canonical and said:
**"If placement is always valid and routing is valid (but may be
incomplete), we should never get a failure."**

That does not pick one of the three options below — it removes the reason
the choice was urgent. The MAX-vs-sum question was load-bearing because
*legality* was riding on a graded margin term, where any `Family.MONEY`
term could zero it out (measured: a $0.046 term overruled
`courtyard_overlap`). Under the ruling, legality is a **hard gate** — an
illegal placement is unstorable, not expensive — so no legality decision
depends on this aggregation any more.

What is left is the narrower, real question the title names: the objective
still grades *preferences* with a MAX over margins and a SUM over money, so
a non-maximal preference contributes nothing while every money term always
counts. That is still surprising to anyone adding a term (and
`pcb-tightest-connected-part.md` is the next item that will trip over it),
but it is now a **tuning-clarity** problem rather than a
manufacturability one. Re-prioritise accordingly: this no longer gates the
always-valid work.

Option (3) — accept and document — is therefore the cheap correct answer
unless someone shows a *preference* being wrongly masked. The
`courtyard_overlap` evidence no longer supports options 1 or 2, because
courtyard overlap is exactly the kind of thing that becomes a gate.

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
