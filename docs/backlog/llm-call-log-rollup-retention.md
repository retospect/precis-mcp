# llm_call_log rollup and retention policy

The `llm_call_log` table ingests ~300k rows/day (measured 2026-08-11; 4.2M
rows/14d aggregate). No retention policy exists. Design a rollup (e.g.,
hourly aggregates by model/placement/source) and raw-row retention window
before the table becomes a vacuum/index problem; coordinate with the
`scripts/db-thrash-review` practice.

## Motivation

- Table is append-only and growing at ~300k rows/day.
- At current rate, retention becomes an index bloat and vacuum pain point
  within months.
- Token columns (shipped 2026-08-11) enable more fine-grained queries; the
  same metric is queryable at multiple granularities.
- Hourly rollups preserve daily/weekly aggregates for billing/reporting
  without keeping every call row.
- Ops practice: `scripts/db-thrash-review` monitors bloat and repack; a
  retention policy feeds that cadence.

## In scope

- Design raw-row retention window (e.g., 30 days, tunable).
- Design rollup granularity and dimensions (model, placement, source, cost
  tier, transport).
- Rollup table schema and indexing.
- Operator runbook for tuning retention post-deploy.
- Integration with `scripts/db-thrash-review` cadence.

## Out of scope

- Retroactive rollup of existing data (backfill if desired, separate task).
- Real-time or sub-hourly rollups.
- Archive/export pipeline.

## Open decisions

1. Retention window duration: 30/60/90 days?
2. Rollup dimensions: should we preserve some raw rows for outlier
   investigation, or aggregate everything?
3. Write the rollup from a cron job, or as a deferred-lane pass?
