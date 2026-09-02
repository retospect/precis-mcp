---
status: draft
title: "disputes adjudication — a claim about two claims, five verdicts, and the derived `contradicts`"
model: opus
blocked-by: disputes-edge-nonblocking-disagreement
---

# Disputes adjudication workflow (Part 2 of the `disputes` split)

Part 1 (`docs/backlog/disputes-edge-nonblocking-disagreement.md`) makes
disagreement free to file: anyone raises a non-blocking `disputes` edge,
and no code path writes `contradicts`. This item builds the resolution
tier that makes `contradicts` mean something again.

## What carries the adjudication: a claim about two claims

Reto, 2026-08-20: *"we have one paper, a nanopub with one claim, and
another nanopub with an opposing claim, and a … nanopub that says A and B
are opposing?"* — yes, and that's the better structure. A `links` row
(`set_by` + a `meta` blob) has no sentence, evidence, author, signature,
or identity: it's a database fact about two rows, not a scientific
statement. **A statement about two claims is itself a claim** — standard
nanopublication practice (assertions whose subject is another
nanopublication, own trusty URI/provenance/signature); the
micropublication model's `supports`/`challenges` and CiTO's
`cito:disagreesWith` are this shape. Making the adjudication a
first-class hub buys, for free, everything hub machinery already does —
authored sentence, `pub_id`, grounding, review, minting, signing,
anchoring — and one more property: **a dispute can itself be disputed.**
"A and B conflict" is falsifiable and often wrong (`scope-mismatch` is
the expected majority verdict); an edge can't record that it was
overturned, a claim can.

### The two tiers

Not competitors — two ends of one lifecycle:

| tier | carrier | who | cost | means |
|---|---|---|---|---|
| **flag** | `disputes` link | anyone, freely (Part 1) | ~zero | "these look like they conflict; someone should look" |
| **adjudication** | a **claim hub** whose sentence is about two hubs | a reviewer, with reasoning | full mint path | "these do/don't conflict, and here is why" |

`contradicts` becomes derived, not authored — a live edge exists because
a signed adjudication hub with verdict `genuine-conflict` says so.

### Verdicts

A `disputes` edge resolves into exactly one of five outcomes. Only the
last blocks:

- `same-claim` → attach evidence to the survivor, retire the duplicate
- `refines` → typed `refines` edge, `disputes` retired
- `scope-mismatch` → different functional / cell size / measurement
  regime; annotate scope on both, no edge. **Expected majority.**
- `unit-error` → one side is arithmetically wrong; retract it
- `genuine-conflict` → `contradicts`, plus a hunt for a third
  adjudicating source

## Scope of work

1. **Second grounding mode — the gate that has to change first.** Every
   claim hub today grounds in a primary-source passage; an adjudication
   hub grounds in *two other claims* plus, usually, a third source.
   `nanopub/gates.py` has no notion of this — as-is an adjudication hub
   is rejected as unsourced. Admit `grounding.mode='claims'` explicitly,
   or the tier is unmintable.
2. **Verdict → effect wiring.** Each verdict's mechanical consequence
   (evidence re-attach, `refines` edge, scope annotation, retraction,
   derived `contradicts`) fires from the adjudication hub, and the
   resolved `disputes` edge is retired — including the confirmation call
   Part 1 removed from the write path: a `genuine-conflict` verdict is
   the BIG-tier `contradiction_confirm` moment.
3. **Skills** — `precis-taproot-help` and `precis-nanopub-help` actively
   *invite* `disputes` (filing is free, expected, harms neither claim)
   and document the five verdicts. A queue for browsing unresolved
   `disputes` edges is needed — `nanopub/overview.py`'s `withheld_count`
   is the analogous existing shape.
4. **Reviewer persona.** `precis-adversarial-reviewer` cannot simply be
   adapted: `scripts/review-paper/run.sh` runs it against a single
   `paper:` handle, and `precis-common-reviewer.md` makes it explicitly
   read-only. Hub adjudication needs pairwise comparison and a write
   capability; of its 7 categories only `unsupported-claim` and
   `overgeneralisation` plausibly transfer. Budget for a new persona or a
   real extension, not a rename.

## Explicitly NOT in scope

- Filing `disputes` at scale — `docs/backlog/claim-conflict-search.md`.
- The cite-misuse relation for (draft-chunk, hub) prose misuse —
  `docs/backlog/contradicts-conflates-evidence-and-prose-misuse.md`.

## Acceptance criteria

- An adjudication hub grounding in two claim hubs (+ optional third
  source) passes admissibility and completes the full mint path.
- Each of the five verdicts produces its mechanical effect and retires
  the `disputes` edge; only `genuine-conflict` yields a live
  `contradicts` (and the pair's mint is then blocked by Part 1's gate).
- An adjudication hub can itself receive a `disputes` edge.
- The unresolved-`disputes` queue lists open flags; resolving one removes
  it from the queue.

## Open questions / decisions log

- Should the adjudication hub's `pub_id` hash the two claim ids alongside
  the sentence? Probably yes — otherwise two adjudications of *different*
  pairs sharing a sentence ("These claims differ in measurement regime.")
  collide into one.
- Does verdict wiring (item 2) run mechanically on mint of the
  adjudication hub, or stay a human follow-through checklist in slice 1?
