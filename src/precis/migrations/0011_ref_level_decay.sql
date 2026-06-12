-- 0011_ref_level_decay.sql
--
-- Model A decay: relevance fade is a property of the data (the ref),
-- not of the tag-application. A memory has ONE decay rate; all its
-- tags share that fate. Tags become pure labels — they carry no
-- decay metadata.
--
-- Trade-off vs Model B (per-ref_tags decay): you can't have "this
-- memory is permanent under user:elmsfeuer but transient under
-- topic:dft". In practice that case is two observations, not one;
-- the simpler model wins.
--
-- Convention: tags ending in `-current:` (or just absent of auto_refresh
-- on the ref) denote contextual relevance. Tags like `user:` /
-- `topic:` are durable categorisations. The preamble buckets by
-- ref-level `auto_refresh_days IS NULL` (durable) vs IS NOT NULL
-- (decaying with weight).
--
-- Two columns added to refs:
--
--   * ``auto_refresh_days INT NULL``
--       NULL  → no decay; ref is permanent (current behaviour).
--       N>0  → ref decays over an N-day window; weight slides from
--              1.0 → 0 (piecewise: flat 1.0 for first half, linear
--              fall to 0 over second half). Refreshable via the
--              ``touch`` verb or by re-putting with the kwarg.
--
--   * ``refreshed_at TIMESTAMPTZ NULL``
--       Initially set to ``created_at`` (or to ``now()`` when first
--       set via touch). Bumped to ``now()`` on every explicit
--       refresh. Used by the weight expression as the
--       last-relevant-at anchor.
--
-- ``expires_at`` is derived: ``refreshed_at + auto_refresh_days
-- days``. Computed in queries; not stored. The existing
-- ``ref_tags.expires_at`` mechanism stays in place — it's still
-- useful for tag-specific ephemerality even though most
-- decay-by-relevance use cases move to the ref level.
--
-- Migration is additive; existing refs get NULL for both columns
-- and behave exactly as before.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE refs
    ADD COLUMN IF NOT EXISTS auto_refresh_days INT,
    ADD COLUMN IF NOT EXISTS refreshed_at      TIMESTAMPTZ;

-- Partial index on refs that actually carry decay metadata. Most
-- refs don't decay, so the index stays small. Queries that filter
-- on ``auto_refresh_days IS NOT NULL`` use it; queries that don't
-- care pay no cost.
CREATE INDEX IF NOT EXISTS refs_auto_refresh_idx
    ON refs (auto_refresh_days, refreshed_at)
    WHERE auto_refresh_days IS NOT NULL;

COMMIT;

-- End of 0011_ref_level_decay.sql
