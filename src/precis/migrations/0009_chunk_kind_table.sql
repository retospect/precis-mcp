-- 0009_chunk_kind_table.sql
--
-- F9 note (2026-06-04): shares the ``0009`` prefix with
-- ``0009_ref_events.sql``. Not a bug — the migration runner
-- (:class:`precis.store.migrate.Migrator`) keys on the full filename
-- stem (``version=path.stem``), so the two files are tracked as
-- distinct migrations. Alphabetical sort applies ``chunk_kind_table``
-- before ``ref_events`` on a fresh DB. Existing prefix collision at
-- ``0003`` is the same pattern (``app_state`` + ``provenance_rw_cache``)
-- and has been live in prod without issue. Future migrations should
-- pick a fresh prefix to avoid the visual ambiguity.
--
-- 1. Register ``table`` as a valid chunk_kind. Marker's pipeline now
--    maps its ``table`` block type to this kind (was previously
--    collapsing into ``paragraph``, which made RAKE produce noise
--    like ``"na na na na"`` on tables with empty cells — see the
--    deng10 MTV-MOF case study).
--
-- 2. Backfill: detect existing paragraph chunks that are actually
--    markdown tables and relabel them. Heuristic — text starts with
--    a pipe and ≥5 % of its non-newline characters are pipes. Tuned
--    on the deng10 corpus: catches the obvious markdown grid without
--    flagging prose that incidentally includes ``|``.
--
-- 3. Dedup: remove byte-identical chunks within the same ref. Keep
--    the lowest ``ord`` so the section_path / page_first stay
--    contiguous-ish for the remaining row.

BEGIN;

-- 1. Register ``table`` as a valid chunk_kind.
INSERT INTO chunk_kinds (slug, is_card, description)
VALUES ('table', false, 'Markdown table emitted by Marker (skip RAKE).')
ON CONFLICT (slug) DO NOTHING;

-- 2. Backfill: paragraph chunks that look like markdown tables.
UPDATE chunks
   SET chunk_kind = 'table'
 WHERE chunk_kind = 'paragraph'
   AND text LIKE '|%'
   AND (length(text) - length(replace(text, '|', '')))::numeric / NULLIF(length(text), 0) > 0.05;

-- 3. Dedup byte-identical chunks within a single ref.
WITH duplicates AS (
    SELECT chunk_id,
           ROW_NUMBER() OVER (
               PARTITION BY ref_id, md5(text)
                   ORDER BY ord
           ) AS rn
      FROM chunks
)
DELETE FROM chunks
 WHERE chunk_id IN (SELECT chunk_id FROM duplicates WHERE rn > 1);

COMMIT;
