---
status: draft
pillar: quests
---

# Quest seed-orphan recovery — ref_events trigger

gr503453, carried over from the spec pruned in 34f08cfff. Two halves of
the original item shipped since:

- Per-tick repair: `quest/compute.py::_stuck_seed_failure` surfaces a
  still-open seed todo whose only jobs are `failed`/`cancelled`, and the
  windowed `_SEED_INFRA_RETRY_WINDOW_HOURS` budget re-mints a fresh seed
  job under the same todo.
- The audit trail: migration `0231_refs_retire_event_trail.sql` (in
  flight on worktree-g2-silk-retire, cites this gripe) appends a
  `('db','retired')`/`('db','restored')` `ref_events` row for every
  `retired_at` transition on every kind — closing the 2026-08-11 hole
  (nine job refs soft-deleted in one tx, zero trail, actor unknown).

What neither half covers: **consuming** the trail. The repair runs
per-tick and only sees shapes its queries enumerate; a soft-delete or
merge that lands between ticks on a node the tick never inspects can
still wedge or silently truncate a quest's fan-out.

## The remaining gap

Every repair and gate query filters `retired_at IS NULL`, so a retired
or merged node vanishes from the tick's view:

- A retired **seed todo** (not just its job) drops out of
  `_stuck_seed_failure`'s enumeration AND out of the
  `child_job_succeeded` gate's live-child count — the aggregate then
  mints on a silently partial fan-out (config asked for N seeds, it
  combines N−1).
- A retired or **merged** candidate/pathway/structure that parent links
  or the serve tree still point at — no per-tick scan looks there at
  all.
- Any raw-psql retire: 0231 now logs it, but nothing reads the log.

## Proposal

A consumer of `ref_events` where `source='db' AND event='retired'` —
a light periodic scan alongside the sweeper is the cheaper option over
a second security-definer trigger chain. For each retired ref, walk
`parent_id` up (quest trees are shallow: quest ← candidate ← T_agg ←
seed todo ← job, ≤4 hops); when an ancestor sits under a
`STATUS:active` quest's serve tree, act:

- seed todo retired → flag the parent `T_agg` so the dispatch path
  re-mints the missing fan-out unit instead of aggregating partials;
- candidate/structure retired or merged → leave a quest note or gripe
  naming the lost ref — no repair can re-derive a human-curated
  candidate;
- job retired under an open seed todo → nothing: the existing per-tick
  repair already covers it; do not duplicate the mint.

## Open questions

- Scan vs trigger: a scan of recent `('db','retired')` rows on the
  sweeper's cadence matches the existing posture; a write-time trigger
  makes detection immediate but adds a second definer function to every
  retire path.
- "Merged" needs its own evidence — check what a merge writes today
  (re-pointed `links`? a `merged-into` event?) before the consumer
  claims to see it.
- Partial-fan-out aggregation may warrant its own guard regardless of
  this item: an aggregate landing on fewer seed partials than the config
  asked for is a silent under-measurement.
