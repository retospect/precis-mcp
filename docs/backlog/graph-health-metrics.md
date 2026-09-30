---
status: idea
pillar: memory-graph
title: Measure graph navigability, not just gardener proposals
---

# Measure graph navigability, not just gardener proposals

What: `graph-gardener.md` proposes merge/split/prune passes over the graph,
but nothing measures whether the graph is navigable in the first place:
hop-reachability from a random hub, orphan rate per kind, evidence-edge
precision (the 367/1498 number `evidence-edge-verification.md` already
computed once, by hand), dedup near-miss rate (`gr180306`). A weekly job
writes these as `measures` rows (once `measures-substrate.md` ships) so the
gardener has a target to move and the roadmap has a number to report against.

Why: every one of these numbers exists today as a one-off audit result
buried in a gripe or a backlog item's prose — none is a standing signal.
`graph-gardener.md` proposes changes with no baseline to show they helped,
and Reto's roadmap review has no health number to point at at all.

Owner anchor: a new weekly pass under `src/precis/workers/` (job type TBD);
consumes the same `refs`/`links`/`chunk_embeddings` tables
`graph-gardener.md` and `evidence-edge-verification.md` already read.

test: one weekly `measures` row per metric (reachability, orphan rate,
evidence-edge precision, dedup near-miss rate), queryable over a time range
so a graph-gardener pass can be shown to move one of them.

Closest existing items: `graph-gardener.md`, `evidence-edge-verification.md`,
`context-quality-eval.md`.
