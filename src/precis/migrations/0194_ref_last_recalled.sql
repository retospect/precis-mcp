-- 0194_ref_last_recalled.sql
--
-- `refs.last_recalled_at` — when an *agent* last read a memory's own view
-- through `get(kind='memory')` (the fisheye or full render of that node, not
-- the neighbour lines it prints). The agent counterpart of
-- `refs.last_viewed_at` (0038), which only the human web reader stamps, so a
-- memory an agent leans on every session looked untouched. Recency of a
-- memory is GREATEST(last_viewed_at, last_recalled_at, updated_at): it orders
-- a hub's member listing and feeds memory-lint's "cold" finding.
--
-- Nullable: never recalled reads as NULL and is ignored by GREATEST. A single
-- PK UPDATE stamps it (`store.touch_recalled`); no index — it is only ever
-- read per row.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE refs ADD COLUMN IF NOT EXISTS last_recalled_at timestamptz;

COMMIT;

-- End of 0194_ref_last_recalled.sql
