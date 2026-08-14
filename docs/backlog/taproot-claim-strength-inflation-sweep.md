---
status: idea
title: Claim-strength (modal) inflation — a third taproot failure class, found by accident and never sized
model: opus
---

# Claim-strength inflation: an unsized failure class

The reground pass over draft 173020 was built around two known failure shapes:
**proxy grounding** (the passage asserts or defers rather than evidencing —
"right paper, wrong chunk" was the dominant real fix) and **term leak** (a term
from an adjacent claim contaminating a hub during minting). A third shape
exists, was found by accident, and has never been searched for.

## The shape

The claim's **modal or epistemic register drifts** while every word remains
present in the source. The source hedges; the claim asserts. Nothing is
fabricated, no citation is wrong, the grounding chunk genuinely is the right
passage in the right paper — and the claim still overstates what the source
says.

## The one known instance

Finding `192706`, live and unresolved, holds a `contradicts` edge onto hub
`fi191316`. Verified 2026-08-14: direction 192706 → 191316, created
2026-08-04 21:03 UTC, no annotation on the link row. Its title states the
defect in full:

> `dc2445944`: `fi191316` claim-strength inflation — "will ultimately require"
> vs source's "could be used"

Note the `dc` prefix: it was raised against a **draft chunk**, i.e. caught on
the drafting side rather than by any hub-side check.

## Why every existing check misses it

- **Evidence grounding** passes — the passage genuinely supports the topic, and
  the strict-judge rubric asks whether the content is primary, not whether the
  certainty matches.
- **The prose pass** passes — the sentence is well-formed and faithful at the
  word level.
- **Term screens** pass — no foreign or leaked vocabulary is involved.

The defect lives entirely in the gap between "could" and "will". This is also
why it is plausible the class is common: nothing in the pipeline is looking.

## Suggested direction

A sweep comparing each hub's claim against its grounding chunk on modal and
hedge strength. A cheap first detector is a lexicon diff — assertive registers
(`will`, `must`, `requires`, `demonstrates`, `shows that`, `establishes`)
appearing in a claim whose grounding passage sits in a hedged register
(`could`, `may`, `suggests`, `indicates`, `is consistent with`, `potentially`).
That is a candidate generator, not a verdict; each hit still needs judging.

Two design points worth deciding up front:

- The existing `contradicts` edge is already the right affordance for recording
  a hit — `192706` demonstrates the shape, so a sweep can emit into an
  established structure rather than inventing one.
- Where the fix belongs is genuinely open. Tightening the hub's claim text is
  one option; the instance above suggests the inflation can equally originate
  draft-side, in which case fixing the hub leaves the draft sentence wrong.

## The meta-finding

That `contradicts` edge has sat live and unresolved for ten days. Nothing
consumes `contradicts` edges — they are written and then not read. Whatever
sizing sweep gets built will produce more of them, so a triage path for the
relation is arguably the prerequisite rather than the follow-on.
