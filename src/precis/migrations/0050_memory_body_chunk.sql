-- 0050_memory_body_chunk.sql
--
-- Move a memory's prose out of `refs.title` and into a body chunk, so the
-- header becomes a short, scannable title and the body lives where every
-- other chunk-backed kind keeps it. This is the `gripe_body` shape applied
-- to `kind='memory'`: `refs.title` holds a title, a `memory_body` chunk
-- (ord >= 0) holds the prose, and the standard embed + chunk_keywords
-- workers index it for free. Dreams are memories, so this is the "dreams
-- live in a chunk, not the ref header" fix.
--
-- It also seeds a `todo_body` slug for the additive todo details-body (no
-- backfill — todo titles are already good headers; the body is new surface).
--
-- Backfill strategy (memory only):
--
--   1. A memory today carries exactly one embedded chunk: its `card_combined`
--      card at ord = -1 (emits_card=True). We *repurpose that chunk in place*
--      rather than delete+reinsert — an UPDATE to ord >= 0 + chunk_kind
--      'memory_body' keeps the chunk_id, so its existing `chunk_embeddings`
--      row survives untouched (content_sha unchanged → the embed worker does
--      not re-claim it). This avoids re-embedding the whole memory corpus.
--      Keywording *does* activate lazily: card_combined is in the
--      chunk_keywords skip-list, memory_body is not, so chunk_keywords picks
--      it up on the next pass — memory clustering turns on for the first time.
--      The new ord is the ref's next free ord (COALESCE(MAX(ord)+1, 0)) so we
--      never collide with a pre-existing tag_overflow chunk at ord 0.
--
--   2. Any memory with no card at all (very old rows) gets a fresh
--      `memory_body` chunk seeded from its title, so the render path always
--      finds a body chunk. Guarded by NOT EXISTS on ord >= 0 so it never
--      double-writes a memory already handled by step 1.
--
--   3. `refs.title` is then truncated to a real title: the first line
--      (existing first-line-is-summary discipline), capped at 80 chars with
--      an ellipsis. The full prose is preserved in the body chunk from step
--      1/2, so nothing is lost.
--
-- content_sha note: like every non-draft chunk, memory_body carries a NULL
-- `chunks.content_sha`; the embed worker aligns `chunk_embeddings.content_sha`
-- to match (NULL), so there is no re-claim/spin-loop. Identical to gripe_body.

BEGIN;

-- 1. Register the two new chunk kinds (FK target for chunks.chunk_kind).
--    is_card = FALSE ties them to ord >= 0 per the chunks_check constraint.
INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('memory_body', FALSE, 'Memory body prose (embedded + keyworded; ord >= 0)'),
    ('todo_body',   FALSE, 'Todo details body (optional; embedded + keyworded)')
ON CONFLICT (slug) DO NOTHING;

-- 2. Repurpose each memory's card_combined card as its body chunk, in place.
--    chunk_id (and its embedding) is preserved; only ord + chunk_kind change.
UPDATE chunks c
SET ord = COALESCE(
        (SELECT MAX(c2.ord) + 1 FROM chunks c2
         WHERE c2.ref_id = c.ref_id AND c2.ord >= 0),
        0),
    chunk_kind = 'memory_body'
WHERE c.ord = -1
  AND c.chunk_kind = 'card_combined'
  AND c.ref_id IN (
      SELECT ref_id FROM refs WHERE kind = 'memory' AND deleted_at IS NULL
  );

-- 3. Seed a body chunk for any live memory that still lacks one (no card).
INSERT INTO chunks (ref_id, ord, chunk_kind, text)
SELECT r.ref_id, 0, 'memory_body', r.title
FROM refs r
WHERE r.kind = 'memory'
  AND r.deleted_at IS NULL
  AND r.title IS NOT NULL
  AND btrim(r.title) <> ''
  AND NOT EXISTS (
      SELECT 1 FROM chunks c WHERE c.ref_id = r.ref_id AND c.ord >= 0
  );

-- 4. Shrink refs.title to a real title: first line, capped at 80 + ellipsis.
--    The body chunk (step 2/3) holds the full prose.
UPDATE refs r
SET title = CASE
        WHEN char_length(split_part(r.title, E'\n', 1)) > 80
            THEN left(split_part(r.title, E'\n', 1), 79) || U&'\2026'
        ELSE split_part(r.title, E'\n', 1)
    END
WHERE r.kind = 'memory'
  AND r.deleted_at IS NULL
  AND r.title IS NOT NULL
  AND btrim(r.title) <> ''
  AND (
      position(E'\n' IN r.title) > 0
      OR char_length(r.title) > 80
  );

COMMIT;
