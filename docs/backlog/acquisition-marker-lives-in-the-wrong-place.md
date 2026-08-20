---
status: draft
title: "the acquisition marker is a provenance state stored inside a text chunk another door legitimately overwrites"
---

# A flag hiding in prose

A chase-born claim hub whose source paper is not in the corpus carries an
acquisition marker — *"Paper not in corpus — needs acquisition"* — as **text
inside its `finding_body` chunk**. The hearsay/acquisition gate
(`src/precis/nanopub/gates.py`, via `ev.ACQUISITION_MARKER`) matches against
`bundle.body` as well as `bundle.sentence` precisely because the title alone
would miss it.

That works only as long as nothing rewrites the body. But for a `TAPROOT:claim`
hub, `finding_body` **is** the sentence copy, and `refine_claim_sentence`
replaces it on every reword — by design, so the dedup ANN index self-heals.

The two facts are incompatible. A reword erases the marker, and with it the
evidence of a provenance condition that has not changed: we still do not have the
paper.

## How it surfaced

Fixing `approve()`'s title-override door (2026-08-20) to route rewords through
`refine_claim_sentence` — correct for gate ordering, body staleness and `pub_id`
identity — meant a review-time reword now erases the marker on the same call that
gates it. A claim about a paper we do not hold could be approved. Closed with a
capture-before-reword fix: read the marker from the pre-reword body and gate on
that state, so the reword cannot launder it.

That fix is correct and sufficient. It is also a patch around the real shape
problem.

## The actual defect

**A provenance state is being stored as prose in a content field.** Anything that
legitimately rewrites the content destroys the state, and the only defence is for
every such writer to remember to carry it forward. There is exactly one such
writer today; there is no guarantee there will be one tomorrow, and nothing fails
loudly when the next one forgets.

The marker belongs on `refs.meta` (or a tag) — somewhere a reword does not touch
and a query can find. Then the gate reads a flag rather than grepping a sentence,
and "is this claim's source actually in the corpus?" becomes answerable in SQL
instead of by substring match.

## What shipped (2026-08-20)

`gates.check_primary_source` now has **three structural arms** ahead of the prose
one, and no migration was needed for any of them — every state was already in the
DB, unread:

- **derived** (`bundle.unheld_sources`) — an evidence source with no live body
  chunk (`ord >= 0 AND retired_at IS NULL`) is a paper we hold the metadata of and
  not the text. Read off `chunks`.
- **awaiting** (`bundle.awaiting_sources` / `bundle.acquiring`) — pure plumbing.
  `put(kind='finding', wants=...)` has written `finding --awaits-evidence-->
  DREAM:acquire stub` plus `STATUS:acquiring` since migration 0105, and `chase`'s
  acquiring arm polls the edge; the mint path simply never looked. `awaits-evidence`
  is deliberately not an evidence relation (the stub supports nothing yet), so
  `seniority.derive_evidence` skips it and the derived arm could never see it.
  Covers the "primary known only by descriptor" shape, and — via the tag —
  the hub whose stub was since soft-deleted.
- **declared** (`refs.meta.primary_source_unheld`) — the one genuinely new write,
  for the shape *no* edge expresses: a claim read out of a **citing** paper we DO
  hold, whose primary never got a `refs` row. `refs.meta` survives a reword;
  `finding_body` does not. Not a migration — `refs.meta` is jsonb.

`approve()` additionally *refuses before the reword*, so a refusal never destroys
the prose marker and a retry is idempotent (the snapshot alone survived exactly
one call).

## What is left: retire the prose arm

The prose regex stays as a fallback for one release. It has **no writer in this
codebase** — it is agent free text — so retiring it needs only one thing to be
true: every live hub whose `finding_body` matches `ACQUISITION_MARKER` carries the
declared flag instead. Six such hubs in prod as of 2026-08-19.

The backfill is idempotent, dry-run by default, and its empty listing IS the
retirement test:

```
precis nanopub backfill-unheld            # dry run: lists the hubs + matched marker
precis nanopub backfill-unheld --apply    # stamp meta.primary_source_unheld
```

(`evidence.prose_marked_hubs` / `declare_primary_source_unheld`. Already-stamped
hubs drop out of the query, so re-running is a no-op and the listing shrinks to
zero.) The prose is left in the body deliberately: `chunks` is append-only for
`ord >= 0`, so rewriting a body means DELETE + INSERT through a registered
synthesis pass and would re-run the embedding cascade to delete a sentence that
harms nothing. Moving the *state* is the point.

Once the dry run prints nothing on prod, delete the `marked = ...` paragraph in
`check_primary_source`, `ACQUISITION_MARKER`, the `provenance_body` parameter it
is the sole reader of, and `approve()`'s pre-reword short-circuit — then close this
item.
