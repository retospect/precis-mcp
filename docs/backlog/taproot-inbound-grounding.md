---
status: idea
pillar: memory-graph
---

# Inbound grounding — corpus backfill (the open remainder)

Intent (Reto): every new paper is checked support/deny against existing
claims. Two of three parts landed:

- (b) shipped: `workers/inbound_ground.py` grounds papers created inside
  `PRECIS_INBOUND_GROUND_MAX_AGE_DAYS` (a9eddd07b, 885bd1ea7).
- (a) — evaluating and flipping `PRECIS_INBOUND_CHASE_ENABLED` for
  `workers/inbound_chase.py` — is an operator decision tracked as a
  fleet todo, not here. Its stated preconditions are now in place: a
  per-paper candidate cap + a UTC-day ceiling on the pass, both
  surfaced on the pass's worker_logs digest line.

What is left is (c): the full papers × claims corpus backfill — papers
older than the max-age window that the shipped pass never visits.
Undesigned: needs a spec before it is buildable (batch order, cost
bounds, watermarking so the pass resumes rather than restarts).
Type-2 general-similarity linking (`related-to` + meta.note) is
deliberately separate scope.
