-- 0262_paper_note_chunk_kind.sql
--
-- Register `chunk_kind='paper_note'` — the append-only note timeline on
-- kind='paper' (gr521094): `put(kind='paper', id=<paper>, text='...')`
-- appends one, `delete(..., args={'comment': K})` hides it, the overview
-- get shows the newest and `view='comments'` lists them. Same shape as
-- `gripe_comment` (0005) / `todo_comment` (0195), one difference:
--
-- A paper note is NOT an ord >= 0 chunk. Paper bodies are rewritten in
-- place — `drop_body` / `reextract` / `move_body_to` / the restore door
-- all `DELETE ... WHERE ord >= 0` and INSERT the new body at ords 0..M —
-- so an ord >= 0 note would either die with the body it annotated or
-- collide with a longer re-ingested body at the same ord (chunks is
-- UNIQUE on (ref_id, ord)). Notes therefore live in their own ord < 0
-- band (ord = -1000000 + K - 1): every body predicate in the codebase
-- is `ord >= 0` (search, TOC, count, has_body, find-in-ref, the body
-- deletes themselves), so the band IS the exclusion — no search,
-- embedding or citation path can read a note as paper body text. The
-- embed / summarize workers claim by chunk_kind/no_index rather than
-- ord, so the handler also stamps meta.no_index='true' and 'paper_note'
-- joins `EmbedHandler.skip_chunk_kinds` / the keyword+summarize skip
-- sets.
--
-- `chunks_check` is widened (not weakened): ord < 0 now admits the
-- `card_%` prefix OR 'paper_note', and ord >= 0 forbids 'paper_note'
-- outright so the band stays exclusive. The rewrite is strictly more
-- permissive, so the new check goes on NOT VALID: the DROP + ADD take
-- ACCESS EXCLUSIVE for a metadata-only moment — no scan, no table
-- rewrite. VALIDATE is the separate 0263 migration: run in this file's
-- transaction it would scan all of `chunks` (~5.3M rows in prod) while
-- the ACCESS EXCLUSIVE locks are still held (the 0214 → 0215 split).
-- `is_card = FALSE`: a note is agent-authored timeline, not a derived
-- card.
--
-- `precis_trust_subject_chunk` needs no update — it returns
-- `p_ord >= 0 AND NOT <timeline kinds>`, so an ord < 0 note is never
-- the subject already (the 0214/0254 classification is ord-gated).
--
-- Forward-only (ADR 0005). Idempotent: ON CONFLICT DO NOTHING; the
-- constraint rewrite is a drop + re-add.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('paper_note', FALSE, 'Append-only note on a paper (put id + text=); ord<0 band, never embedded or read as body')
ON CONFLICT (slug) DO NOTHING;

-- Widened, not removed: re-added on the next line, validated by 0263.
-- squawk-ignore ban-drop-constraint
ALTER TABLE chunks DROP CONSTRAINT chunks_check;
ALTER TABLE chunks ADD CONSTRAINT chunks_check CHECK (
    (
        (ord < 0)
        AND ((chunk_kind ~~ 'card_%'::text) OR (chunk_kind = 'paper_note'::text))
    )
    OR (
        (ord >= 0)
        AND (chunk_kind !~~ 'card_%'::text)
        AND (chunk_kind <> 'paper_note'::text)
    )
) NOT VALID;

COMMIT;

-- End of 0262_paper_note_chunk_kind.sql
