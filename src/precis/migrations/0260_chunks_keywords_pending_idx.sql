-- 0260_chunks_keywords_pending_idx.sql
--
-- chunk_keywords drain fix (gr520236): the fresh-backlog claim is
-- `WHERE keywords IS NULL ... ORDER BY chunk_id DESC` (newest-first, so
-- fresh ingests get labels first). Without an index covering it, every
-- claim re-walks the index entries between the id top and the live
-- backlog — the same "rescan" cost the old ascending claim paid at id
-- 1, mirrored. Partial on `keywords IS NULL` only: the eligibility
-- filters (skip-kinds, length floor, meta.no_index) stay in the claim
-- query so they can change without invalidating this index — a row is
-- in the index iff it has never been keyworded.
--
-- Plain CREATE INDEX: Migrator.apply_all() wraps each file in a
-- transaction (CONCURRENTLY cannot run there), and migrations apply in
-- the deploy's single-writer window. A partial btree over ~1.5M
-- unkeyworded rows holds a SHARE lock on chunks while it builds —
-- tens of seconds on prod, per the 0255/0208 precedent.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- at release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS chunks_keywords_pending_idx
    ON chunks (chunk_id DESC)
    WHERE keywords IS NULL;

COMMIT;

-- End of 0260_chunks_keywords_pending_idx.sql
