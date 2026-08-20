---
status: draft
title: "the merge door — no primitive exists for collapsing two hubs that both already exist"
---

# There is no merge

`place()`'s action vocabulary is `attach | new | new_contradicts | needs_review`.
On a duplicate it *attaches* evidence to the hub that already exists — it
prevents a second hub being minted. It has no path for two hubs that both
already exist, and `apply_placement`'s docstring is explicit that a risky merge
is never auto-applied (it files a todo instead). What exists for repair is
`refine_claim_sentence` (reword one hub in place, keeps the ref and its edges,
recomputes `pub_id`) and `taproot refine --from/--to` (link sharper→coarser).
Neither moves evidence.

So the backward sweep in `claim-hub-dedup-sweep.md` has no door. Correcting that
file's claim that merging is "what `place()` would have done": it is not —
`place()` would have stopped the loser existing.

## What the door must do

Collapse loser into winner. The hard part is not the retire, it is the edges.

**Repoint every link on the loser, both directions.** Inbound: evidence edges
from sources (`corroborates`, `establishes`, `contradicts`) and — easy to
forget — `cites` edges from *draft* chunks. A draft citing the loser must end up
citing the winner or the draft silently loses its reference. Outbound: `refines`,
`contradicts`, `conjunct-of`.

**Four cases the naive repoint gets wrong:**

1. **Collision.** Winner already has an edge to the same peer with the same
   relation. Dedup in the tool, on `(src_ref_id, dst_ref_id, relation)` and on
   `src_chunk_id` where the edge is chunk-grounded — two chunk-grounded cites
   from different chunks are *not* redundant.

   The DB does guard this: `links_endpoints_relation_idx` is a unique index on
   `(src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation) NULLS NOT
   DISTINCT`, live since `0001_initial.sql`, and `Store.add_link`'s `ON CONFLICT`
   already targets it. The tool still needs its own dedup so the dry-run can
   *itemize* which edge collided with which, rather than silently relying on
   `DO NOTHING` — but a same-chunk duplicate cannot reach the table.

   **The trap that made us believe otherwise, worth remembering:** a plain
   `CREATE UNIQUE INDEX` does not appear in `information_schema.table_constraints`
   — only constraints declared with `CONSTRAINT` syntax do. A prod check against
   that view reported "no unique constraint" and a whole backlog item was written
   on it (deleted 2026-08-20; `git log` keeps it). Query `pg_indexes` /
   `pg_catalog` when asking whether a uniqueness guarantee exists.
2. **Self-loop.** If winner and loser are linked to each other — a `contradicts`
   between near-duplicates is plausible — repointing yields an edge from the
   winner to itself. Drop it.
3. **No soft-delete on links.** The `links` table has no `deleted_at` column at
   all, so removing a redundant edge is a hard DELETE. There is no undo; the
   dry-run is the only safety net, which is why it is mandatory rather than
   polite.
4. **Grounding rows.** A reopen NULLed `grounding` on the publish rows once
   already; check whether any hand-trimmed quote/snip hangs off the loser's
   edges before discarding them.

**Refuse rather than guess** when either side is past `candidate` in
`nanopub_publish`. Merging changes `pub_id`, and a `reviewed`/`signed` artifact
cannot be retroactively re-identified. Today all rows are `candidate`, so this
guard costs nothing and stops the pass being run later at the wrong moment.

**Record the merge.** The loser is soft-deleted (`refs.deleted_at`) with a link
to the winner so the collapse is traceable and idempotent. Reuse whatever
`taproot refine` already writes rather than inventing vocabulary — note
`supersedes`/`superseded-by` are live but reserved for nanopub *artifact*
versioning, so overloading them for hub identity is likely wrong.

**Idempotent, dry-run-first.** Re-running is a no-op. The dry-run prints every
edge it would move, drop as redundant, or drop as a self-loop, and the resulting
publish-state check — reviewed by a human before the first write. Standing rule:
a confidence floor, an idempotency story and a dry-run before any corpus-wide
automated writer commits.

## Scope note

The winner's `pub_id` is unchanged if its sentence and scope are untouched.
Rewording the winner for lint compliance is a *separate* operation
(`refine_claim_sentence`) and changes `pub_id` — do not fold the two together, or
a failed reword takes the merge with it.
