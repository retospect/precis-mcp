-- 0024_watching.sql
--
-- Foundation for the watching capability (see docs/design/watching.md):
-- a second "attention actor" over the SAME salience field the dreamer
-- already maintains. Dreaming and watching both select the most-due
-- salient chunk via argmax(last_seen - last_<actor>); the only new
-- storage is a per-actor rotation stamp for the watcher.
--
-- Additive only; forward-only (ADR 0005). Idempotent so a re-run after
-- a partial apply is safe.
--
-- Changes:
--
--   1. `chunks.last_watched` — the watcher's rotation stamp, exact
--      mirror of `last_dreamt` (0007_dreaming.sql). METADATA-ONLY, same
--      as the other salience columns: no content, no embedding/summary
--      cascade, so mutating it does not breach the chunks-body
--      append-only invariant.
--
--   2. Selection-key index on argmax(last_seen - last_watched), mirror
--      of `chunks_dream_score_idx`, so the watch-seed query stays off a
--      seq scan as the corpus grows.

BEGIN;

-- 1. Watcher rotation stamp (metadata-only).
ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS last_watched timestamptz NOT NULL DEFAULT now();

-- Everything starts un-watched at its birth (same rationale as
-- last_dreamt): the column DEFAULT now() would stamp existing rows at
-- apply time, which is wrong for date-rotation selection.
UPDATE chunks SET last_watched = created_at;

-- 2. Selection-key index: argmax(last_seen - last_watched) over target
--    kinds, mirror of chunks_dream_score_idx.
CREATE INDEX IF NOT EXISTS chunks_watch_score_idx
    ON chunks ((last_seen - last_watched) DESC);

COMMIT;

-- End of 0024_watching.sql
