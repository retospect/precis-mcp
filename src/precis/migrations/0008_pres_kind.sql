-- 0008_pres_kind.sql
--
-- Register `kind='pres'` for slide decks and unpublished writeups.
--
-- Motivation: distinguish "academic library" (paper, ~2500 ingested
-- PDFs) from "internal slide decks + drafts + course notes" that
-- the user wants searchable alongside but not muddled into the
-- papers corpus. A `pres` ref carries presentations the agent
-- wrote / consumed / cited but that aren't published academic work
-- the citation chase should fan out from.
--
-- Subtypes carried as open tags on the ref (`subtype:slides`,
-- `subtype:writeup`, `subtype:notes`, …) so the closed-vocab
-- axis stays empty (same shape as `conv`).
--
-- Schema additions are data-only — every column used by `pres`
-- already exists on the shared `refs` + `chunks` tables. This
-- migration only seeds the kind registry and one new chunk_kind
-- for per-slide blocks. `pres` reuses the existing `paragraph`
-- chunk_kind for non-slide bodies (writeups, notes), so most
-- ingest paths keep the default.
--
-- Forward-only (ADR 0005). Statements are idempotent under
-- `ON CONFLICT DO NOTHING` so a re-run after a partial apply is
-- safe.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('pres', FALSE, 'Presentation',
     'Slide deck, unpublished writeup, or other internal document '
     'we want indexed but kept separate from the academic paper '
     'library. Slug-addressed; one block per slide (or per '
     'paragraph for writeups). Subtype carried as ``subtype:slides|'
     'writeup|notes|...`` open tag; ``venue`` and ``date`` live in '
     'meta. See ``precis-pres-help``.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('pres_slide', FALSE,
     'Single slide of a deck (one chunk per slide). Distinct from '
     '``paragraph`` so renderers can show slide numbers and so '
     'cross-kind search hits can be labelled as slides.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0008_pres_kind.sql
