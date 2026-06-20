-- 0031_draft_kind.sql
--
-- ADR 0032 — the `draft` kind: an editable, chunk-native authored
-- document. A draft is the living source of a project's write-up; it
-- exports to LaTeX/PDF/Word with Postgres canonical. Unlike `paper`
-- (frozen, append-only body chunks), a draft's chunks are mutable in
-- structure (reorder/reparent via `pos` + `parent_chunk_id`) and — via a
-- single edit helper — in text (re-derived through `content_sha`).
--
-- This migration is **additive and behaviour-preserving** for every
-- existing kind: papers leave the new columns NULL and behave exactly as
-- before. The new derived-row `content_sha` claim clause compares
-- `IS DISTINCT FROM`, and NULL-vs-NULL never fires, so paper claims are
-- untouched.
--
-- Adds (all nullable / IF NOT EXISTS):
--   * kind 'draft' (named, like paper);
--   * chunk_kinds 'table', 'aside', 'listing', 'term'
--     (paragraph/heading/figure/equation/caption already exist);
--   * chunks.handle          — global opaque 6-char base-58 anchor (the
--                              only exposed chunk handle for drafts);
--   * chunks.pos             — sibling-scoped fractional ordering key
--                              (the DFS reading order); `ord` stays as a
--                              satisfy-the-constraint insertion serial;
--   * chunks.parent_chunk_id — adjacency-list hierarchy (a heading owns
--                              its content + sub-headings);
--   * chunks.content_sha     — hash of the resolved-for-search text;
--                              drives per-consumer re-derivation;
--   * chunks.retired_at      — soft-delete marker;
--   * chunk_embeddings.content_sha / chunk_summaries.content_sha — the
--                              sha each derived row was built against;
--   * chunk_events           — append-only per-chunk lifecycle / undo /
--                              provenance log.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- 1. the ref kind ----------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('draft', FALSE, 'Draft',
     'Editable, chunk-native authored document (ADR 0032). The living '
     'source of a project''s write-up; exports to LaTeX/PDF/Word with '
     'Postgres canonical. Body chunks are mutable in structure '
     '(reorder/reparent via pos + parent_chunk_id) and in text (via the '
     'edit helper + content_sha re-derive). Named ref; chunks addressed '
     'by an opaque ¶<handle>. One draft per project; freeze = snapshot. '
     'See precis-draft-help.')
ON CONFLICT (slug) DO NOTHING;

-- 2. new chunk_kinds (paragraph/heading/figure/equation/caption exist)
INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('table',   FALSE,
     'Draft table — legend as face (text), cell data / LaTeX tabular as '
     'payload (meta).'),
    ('aside',   FALSE,
     'Draft aside / callout box (admonition; tcolorbox/mdframed on '
     'export).'),
    ('listing', FALSE,
     'Draft code listing — verbatim code payload, optional caption '
     'face.'),
    ('term',    FALSE,
     'Glossary term — definition as face (text), {short, long, '
     'surface_forms} in meta; lives in a draft glossary subtree.')
ON CONFLICT (slug) DO NOTHING;

-- 3. mutable-structure + freshness columns on chunks -----------------
ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS handle          TEXT,
    ADD COLUMN IF NOT EXISTS pos             TEXT,
    ADD COLUMN IF NOT EXISTS parent_chunk_id BIGINT
        REFERENCES chunks (chunk_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS content_sha     TEXT,
    ADD COLUMN IF NOT EXISTS retired_at      TIMESTAMPTZ;

-- global, opaque handle: unique across all chunks (sparse — drafts only)
CREATE UNIQUE INDEX IF NOT EXISTS chunks_handle_key
    ON chunks (handle) WHERE handle IS NOT NULL;

-- hierarchy walk + sibling-ordered reading-order lookups
CREATE INDEX IF NOT EXISTS chunks_parent_chunk_id_idx
    ON chunks (parent_chunk_id) WHERE parent_chunk_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS chunks_reading_order_idx
    ON chunks (ref_id, parent_chunk_id, pos) WHERE pos IS NOT NULL;

-- 4. per-consumer content_sha on derived rows ------------------------
ALTER TABLE chunk_embeddings ADD COLUMN IF NOT EXISTS content_sha TEXT;
ALTER TABLE chunk_summaries  ADD COLUMN IF NOT EXISTS content_sha TEXT;

-- 5. per-chunk lifecycle / undo / provenance log ---------------------
CREATE TABLE IF NOT EXISTS chunk_events (
    event_id    BIGSERIAL PRIMARY KEY,
    chunk_id    BIGINT NOT NULL
        REFERENCES chunks (chunk_id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_kind  TEXT NOT NULL
        CHECK (event_kind IN
            ('created', 'edited', 'moved', 'reparented', 'retired',
             'restored')),
    content_sha TEXT,
    prev_text   TEXT,
    source      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS chunk_events_chunk_id_idx
    ON chunk_events (chunk_id, ts);

COMMIT;

-- End of 0031_draft_kind.sql
