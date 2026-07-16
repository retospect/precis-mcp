-- 0068_chunks_forbid_body_text_update.sql
--
-- Enforce the "body chunks are append-only" rule at the DB layer.
--
-- The `chunks` table carries two invalidation models. Body rows
-- (paper / plaintext / memory_body / …: `ord >= 0`, `content_sha IS NULL`)
-- re-derive their `chunk_embeddings` / `chunk_summaries` / `keywords` by ROW
-- IDENTITY: the derived rows only re-enter the worker queues when the chunk
-- row is DELETEd and a fresh one INSERTed. An in-place `UPDATE chunks.text`
-- on such a row therefore leaves the vector, the RAKE/LLM summary and the
-- discovery keywords describing the OLD text, and nothing ever repairs them —
-- search silently serves the pre-edit prose. This was convention-only
-- (AGENTS.md "Don't mutate body chunks"); this trigger makes it enforced.
--
-- The two sanctioned in-place text-edit paths are deliberately EXCLUDED:
--   * draft-family chunks (draft / plan / figure) carry a NON-NULL
--     `content_sha`; `edit_text` bumps it, and the embed/summary workers
--     compare `chunk_embeddings.content_sha` to re-derive. → `content_sha`
--     conjunct excludes them.
--   * card chunks (`card_*`) live at `ord < 0`; `precis.ingest.cards.
--     rewrite_cards` rewrites their text in place but DELETEs the matching
--     `chunk_embeddings` and nulls `keywords`/`keywords_meta` in the same
--     transaction. → `ord >= 0` conjunct excludes them.
--
-- The sanctioned way to change a body chunk's text is DELETE + INSERT (the FK
-- ON DELETE cascade tears down the derived rows; the fresh INSERT re-queues
-- them). DELETE + INSERT never fires this BEFORE UPDATE trigger.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.
-- See docs/design/chunk-append-only-trigger.md.

BEGIN;

CREATE OR REPLACE FUNCTION chunks_forbid_body_text_update()
    RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'chunks.text is append-only for body rows '
        '(chunk_id=%, ref_id=%, ord=%, kind=%): an in-place UPDATE orphans '
        'chunk_embeddings/chunk_summaries/keywords. DELETE the row and INSERT '
        'a fresh one so the derived cascade re-runs (AGENTS.md '
        '"Don''t mutate body chunks").',
        OLD.chunk_id, OLD.ref_id, OLD.ord, OLD.chunk_kind
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS chunks_forbid_body_text_update ON chunks;

CREATE TRIGGER chunks_forbid_body_text_update
    BEFORE UPDATE ON chunks
    FOR EACH ROW
    WHEN (
        NEW.text IS DISTINCT FROM OLD.text
        AND OLD.ord >= 0
        AND OLD.content_sha IS NULL
    )
    EXECUTE FUNCTION chunks_forbid_body_text_update();

COMMIT;
