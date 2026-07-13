-- 0060_anki_kind.sql
--
-- The `anki` kind — spaced-repetition **cloze** cards that live in the corpus
-- and (slice 2) sync to AnkiWeb. Supersedes the thin, half-wired `flashcard`
-- kind: `flashcard`'s only "smarts" was an SM-2 scheduler that never wrote and
-- is redundant with Anki, which owns scheduling. See
-- docs/design/anki-integration.md + docs/proposals/anki-cloze-kind.md.
--
-- A numeric-id ref (like `flashcard`/`memory`): the card body is cloze markup
-- (`{{c1::…}}`) in `refs.title`; `refs.meta` carries a **generic** note shape
-- so a future non-cloze notetype needs no migration:
--
--   meta = {
--     "notetype": "Cloze",              -- the only authored type in slice 1
--     "deck":     "Precis",
--     "fields":   {"Text": "<cloze markup>", "Back Extra": "<terse note>"},
--     "anki":      {...},               -- sync-state, written by slice 2
--     "anki_stats":{...}                -- decay signal, read back by slice 2
--   }
--
-- On create the handler emits the existing `card_combined` chunk (ord=-1) —
-- reused from memory/flashcard, no new chunk_kind — built from the
-- *markup-stripped* text (+ Back Extra) so cards embed + keyword-index and turn
-- up in search. `corpus_role` is 'none' equivalent (an authored artifact, never
-- cited as evidence); the kinds table has no such column, so that is enforced
-- by the handler/export guard, not here.
--
-- Slice 1 is ADDITIVE — it does NOT retire `flashcard` (that is a follow-up
-- commit on this branch: unregister the handler + drop the skill + scrub the
-- kinds table references; 0 live flashcard refs, so no data migration).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- 1. the ref kind ----------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('anki', TRUE, 'Anki card',
     'A spaced-repetition cloze card ({{c1::…}}) that lives in the corpus and '
     'syncs to AnkiWeb. Numeric-id ref; body is cloze markup, meta carries the '
     'generic Anki note shape (notetype/deck/fields). Anki owns scheduling — '
     'no SM-2 here. Supersedes flashcard. See precis-anki-help.')
ON CONFLICT (slug) DO NOTHING;

-- Reuses the existing `card_combined` chunk_kind (from 0001) for the embeddable
-- search card — no new chunk_kind needed.

COMMIT;
