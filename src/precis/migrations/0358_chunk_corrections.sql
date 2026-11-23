-- 0358_chunk_corrections.sql
--
-- First-class correction record for glyph-damaged extracted passages
-- (gr724751). A human-confirmed (or context-inferred) fix to OCR damage —
-- a lost μ, a mangled sign, a misread token — was previously kept only as
-- a free-text "OCR CORRECTED BY HUMAN" paper note: reads of the chunk met
-- the damaged text with no pointer, and the record died with nothing to
-- anchor it to. This table is the durable form: one row per corrected
-- span, anchored on the PAPER plus the QUOTED extracted span (not on the
-- chunk handle, gr690836 — re-extraction mints fresh chunk handles, but a
-- chunk whose text still contains the span still matches). The body text
-- itself stays as extracted; every read surface renders the record under
-- the passage and the consumers (pin term check, draft glyph-cite hint,
-- measure glyph_suspected flag) honour it.
--
-- tier: 'human-read' — a person read the PDF and stated the correction;
--       'inferred' — settled from the paper's own text (abstract, caption,
--       arithmetic), no human read of the damaged token;
--       'confirmed-misprint' — the extraction was RIGHT and the printed
--       source is wrong (the record then guards the printed value).
--
-- Forward-only (ADR 0005). One transaction.

BEGIN;

CREATE TABLE IF NOT EXISTS chunk_corrections (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    -- Optional pointer at the chunk the span was read from. Advisory
    -- only — matching is by span text so re-extraction keeps the record.
    chunk_id bigint REFERENCES chunks(chunk_id) ON DELETE SET NULL,
    page integer,
    extracted_span text NOT NULL,
    corrected_span text NOT NULL,
    tier text NOT NULL CHECK (tier IN (
        'human-read', 'inferred', 'confirmed-misprint')),
    reader text,
    approved_by text,
    source_file text,
    note text,
    created_by text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chunk_corrections_ref_idx
    ON chunk_corrections (ref_id, created_at);

COMMIT;

-- End of 0358_chunk_corrections.sql
