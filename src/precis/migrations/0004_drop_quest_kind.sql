-- 0004_drop_quest_kind.sql
--
-- Retire the `quest` ref kind. Quest was envisioned as an
-- inter-agent task queue, but the consumer side never landed: no
-- worker ever claimed a quest row, so the kind has been write-only
-- in practice. The "papers needing a PDF" workflow that quest was
-- originally pitched for is already covered by the stubs pipeline
-- (chase + fetch_oa + `precis stubs`). The only producer left was
-- the patent_watch "quest mode" composer, which has a complete
-- alternative in `auto_get` mode that ingests directly.
--
-- This migration is forward-only (see ADR 0005):
--
--   1. DELETE FROM refs WHERE kind='quest';
--      Cascades clean every dependent row — chunks (and through
--      them chunk_embeddings, chunk_summaries, chunk_tags),
--      ref_events, ref_tags, ref_identifiers, ref_artifacts,
--      links (both src and dst), cache_state. All declared
--      ON DELETE CASCADE in 0001_initial.sql.
--   2. DELETE FROM kinds WHERE slug='quest';
--      Safe once step 1 has removed every referencing refs row
--      (refs_kind_fkey is ON UPDATE CASCADE only — DELETE is the
--      default RESTRICT).
--   3. DELETE the `quest_body` registry row from chunk_kinds.
--      quest_body was registered but never materialised, so the
--      defensive pre-delete on chunks is a no-op on every known
--      install; we keep it for symmetry / forward safety.
--   4. ALTER TABLE patent_watches DROP COLUMN auto_get.
--      With quest mode gone the column has only one meaningful
--      value (true). The runner now unconditionally takes the
--      ingest-direct path. Existing rows that were sitting at the
--      false default get promoted to auto-get by virtue of the
--      column disappearing.
--
-- Idempotent: every statement is `IF EXISTS` / `DELETE` without
-- error if the target is already absent. A future squashed
-- 0001_initial.sql that omits quest from the seed data lets this
-- migration run cleanly as a no-op.

BEGIN;

DELETE FROM refs WHERE kind = 'quest';
DELETE FROM kinds WHERE slug = 'quest';

DELETE FROM chunks WHERE chunk_kind = 'quest_body';
DELETE FROM chunk_kinds WHERE slug = 'quest_body';

ALTER TABLE patent_watches DROP COLUMN IF EXISTS auto_get;

COMMIT;

-- End of 0004_drop_quest_kind.sql
