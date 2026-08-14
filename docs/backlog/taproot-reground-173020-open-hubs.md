---
status: draft
title: Taproot reground (draft 173020) — two residual hubs, both with defects beyond their sourcing
model: opus
---

# Reground residual: `fi189542` and `fi189543`

The manual reground pass over draft 173020 is complete — evidence, prose and
hub-claim passes all applied and verified on prod (123 hubs; 108 edge adds, 36
prunes, 24 draft rewrites, 2 hubs retitled). These two hubs are the whole
residual. Both were classified `NEEDS_EXTERNAL` by the scout because their
primaries are paywalled and absent from the corpus — but closer reading found
that **each also carries a defect that re-grounding alone would not fix**. That
is the reason they are written down rather than left to the next pass.

## The hazard that makes this urgent

Neither hub is distinguishable from a healthy one in the database. Verified
2026-08-14:

```
189542: TAPROOT:claim, STATUS:canonical, prio 1   unacquirable=no   live edges=1
189543: TAPROOT:claim, STATUS:canonical, prio 1   unacquirable=no   live edges=1
```

`STATUS:canonical`, one live evidence edge each, no `unacquirable` marker.
Anything reading prod — a later reground pass, a citation chase, the drafting
loop — sees two settled claims. Every trace of the problem lives outside the
database. **If these two get marked, this file is mostly redundant; until then
it is the only thing standing between the defects and a reader who trusts
`STATUS:canonical`.**

## `fi189542` — the stated angle is arithmetically wrong

The claim gives nanocone opening angles of approximately 19°, 39°, 60°, **85°**
and 113°. These follow from the disclination formula α = 2·arcsin(1 − N/6) for
N = 1…5 pentagons. At N = 2 that evaluates to **83.6°**, not 85°. The value
appears to be rounding propagated through a review rather than a measurement.

This is independent of the grounding question: the number needs correcting
whichever source the hub ends up attached to. It is also exactly the class of
error the evidence pass cannot catch — the pass judges whether a passage
*supports* a claim, not whether the claim's arithmetic is right.

The naming primary is Krishnan et al. 1997 (`10.1038/41284`), paywalled and not
in corpus.

## `fi189543` — two claims welded together, one of them unsupported

The claim couples a definition to a quantifier. The **definitional half** is
groundable in-corpus *today*, on a strictly better passage than its current
grounding: `pc972022` and `pc972025` (paper ref 5828). No external acquisition
is needed for that half.

The **"most abundant" half** is unsupported by anything in corpus and may
simply be false. The recommendation is to drop the quantifier rather than chase
a source for it — the claim is solid without it and the pass goal is
solidly-supported facts, not defending strong wording.

The naming primary is Iijima et al. 1999 (`10.1016/S0009-2614(99)00642-9`),
also paywalled.

## Handling notes for whoever picks this up

`unacquirable_note=` with `unacquirable_mode='abstract'|'vouched'` is the
affordance designed for exactly this situation and is the natural way to get
both hubs off `STATUS:canonical`. Applying it is an authorial assertion about
the claims, so it wants a human decision, not an automated pass.

Two mechanical gotchas, both paid for once already:

- `edit(kind='finding', …)` accepts **exactly one of** `pick_candidate=` |
  `title=` | `unacquirable_note=`. It supports neither find-replace nor
  `dry_run`. So the `fi189542` angle correction and its unacquirable note are
  necessarily two separate calls, and the correction must pass the **full**
  replacement claim string.
- `refs.title` is capped at 200 characters and only mirrors the claim; the
  authoritative text is the `ord=0` chunk. A retitle whose change falls past
  character 200 produces before/after echo lines that look identical. Verify
  against the chunk, not the echo.

A retitle goes through `hub.py::refine_claim_sentence`, which does a
DELETE+INSERT of the `ord=0` chunk so the embedding and summary cascades re-run,
and keeps the old `pub_id` as an alias so existing cites still resolve.

## Cross-dependency

Both hubs are downstream of the corrupted bibliography entry recorded in
`citation-matcher-title-mismatch.md` — paper ref 783, chunk `pc64792`,
reference [15] carries Krishnan 1997's authors, journal, volume and pages under
an unrelated title. A citation chase for `fi189542`'s primary that routes
through that entry lands on the wrong paper. Read that item first.
