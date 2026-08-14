---
status: idea
title: DB consolidation audit — decay/vacuum policy for the ref corpus (gr51184)
---

# DB consolidation audit (gr51184)

Reto's original flag: the precis DB "feels sprawling" — organic growth with
no pruning discipline degrades search, recall, and embedding quality even as
the corpus grows. Consolidated here from gripe 51184 (2026-07).

Snapshot 2026-08-14 (`refs` per kind, total / soft-deleted): anki
**100,391 / 93,127**, job **40,814 / 40,079**, paper 29,184 / 343, memory
11,999 / 2,293, news **5,249 / 0**, agentlog 3,940 / 418, todo 3,622 / 1,382,
alert **2,306 / 0**. Soft-deleted rows are never hard-purged, and two kinds
(news, alert) have no deletion path at all.

Open questions (from the gripe, still unanswered):
- Which kinds carry dead/low-value refs, and what's the hard-purge policy
  for old soft-deleted rows (anki + job alone are ~133k dead rows dragging
  every `refs` scan)?
- Do news/alert need a retention window (they only ever grow)?
- Near-duplicate memories: is a periodic merge pass needed beyond dreaming?
- Should there be a recurring `db_consolidate`-style job, or is this a
  quarterly manual sweep?

First slice when picked up: a hard-purge pass for soft-deleted anki/job rows
older than N days (cheap, mechanical, biggest win), then decide retention
for news/alert. Related cadences already standing: gripe-gc,
db-thrash-review (index/bloat side — this item is row-count/policy side).
