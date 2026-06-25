-- 0038_ref_last_viewed.sql
--
-- `refs.last_viewed_at` — when a ref was last *opened* in the web reader (a
-- human access stamp, distinct from `updated_at` which moves on writes). The
-- drafts list (`GET /drafts`) sorts by it so the most recently opened draft
-- sits on top. We deliberately do NOT derive this from `MAX(chunk_events.ts)`
-- (a whole-events-table scan) nor bump it on the live-poll endpoints — only
-- the full reader page-load stamps it (`store.touch_viewed`).
--
-- Nullable: a never-opened ref sorts last (the `viewed_desc` order is
-- `last_viewed_at DESC NULLS LAST, updated_at DESC`). General to all kinds,
-- but only the draft reader stamps it today.
--
-- Forward-only (ADR 0005). Regenerate the baseline snapshot at release
-- (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE refs ADD COLUMN IF NOT EXISTS last_viewed_at timestamptz;

COMMIT;

-- End of 0038_ref_last_viewed.sql
