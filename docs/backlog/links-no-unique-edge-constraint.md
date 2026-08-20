---
status: draft
title: "`links` has no unique constraint on (src, dst, relation) — any corpus-scale edge writer must supply its own idempotency"
---

# The same edge can be written twice

Measured against prod, 2026-08-20:

```sql
SELECT constraint_name, constraint_type FROM information_schema.table_constraints
WHERE table_name = 'links' AND constraint_type IN ('UNIQUE', 'PRIMARY KEY');
-- links_pkey | PRIMARY KEY   (on link_id)   ← the only one
```

There is no unique index on `(src_ref_id, dst_ref_id, relation)`. Two
identical edges differ only by `link_id`.

## Why it has not bitten yet

Every relation that matters today is written either by hand or by a path
that runs once per (claim, candidate) pair inside a single job. The whole
`contradicts` census is 6 rows. Nothing has re-run over the same pairs.

## Why it is about to matter

Two planned passes write edges over the whole corpus, repeatedly:

- **Evidence widening** — retrieve paper chunks near a hub, judge them,
  attach the survivors as `establishes`/`corroborates`. This is inherently
  re-runnable: the corpus grows, so you want to re-run it as new papers
  land. A second run over an unchanged hub re-attaches every supporter it
  already attached.
- **Hub dedup / re-placement** after the `block()` index repair
  (`taproot/canon.py`), which takes candidate-retrieval coverage from
  12.3% to 100% and makes `place()` reachable for hubs it never saw.

A duplicated evidence edge is not cosmetic. Supporter counts feed the
confidence signal on the claim page, and
`handlers/_finding_evidence.py`'s independent-supporter union-find counts
*edges* grouped by author — so a doubled edge inflates the apparent
weight of a claim. The failure is silent and it runs the wrong way:
it makes a claim look better-supported than it is.

## Options

1. **Add the unique constraint** (forward-only migration) and make writers
   `ON CONFLICT DO NOTHING`. Cleanest, but needs a dedup of any existing
   duplicates first, and needs a decision on whether `meta`/`set_by`
   differences should count as distinct edges (probably not).
2. **Idempotency in each writer** — check-then-insert inside the
   transaction. Weaker (races), and every new writer must remember.
3. **Both** — constraint as the durable guard, `ON CONFLICT` in writers so
   they do not raise on a benign re-run. This matches how the codebase
   treats the `chunks` append-only invariant: DB-level guard plus a
   cooperative writer.

Option 3 is the recommendation.

## Check first

- Are there existing duplicate `(src, dst, relation)` rows in prod? The
  2026-08-20 check found none for `contradicts` specifically; run it
  across **all** relations before adding a constraint, since the migration
  fails on existing duplicates.
- Does any legitimate use case want two edges of the same relation between
  the same pair — e.g. the same paper supporting a claim via two different
  passages? If grounding lives on the link row rather than in a separate
  column, a unique constraint on the triple would wrongly collapse them.
  **This is the question that decides whether option 1 is even correct** —
  resolve it before writing the migration.
