-- 0241_paper_bib_entries_oa_source.sql
--
-- gr513755 slice 2: OpenAlex `referenced_works` as a *source* of
-- `paper_bib_entries` rows. `bib_parse` (migration 0108) is the only
-- writer today, so the table carries no provenance column — every row
-- is implicitly parse output. The OpenAlex backfill writes one row per
-- referenced work for papers bib_parse never parsed (it runs at ~3.6
-- papers/h via Crossref; the free OpenAlex work object carries the
-- whole reference list in one call). `source` marks which writer made
-- the row ('bib_parse' default covers all existing rows; 'openalex'
-- for the new path) and `openalex_id` keeps the cited work's W-id —
-- the row's authoritative identity for OpenAlex-sourced entries, which
-- carry no DOI/s2_id and may never resolve to a held ref (the slice-3
-- stub trickle reads this column for its unheld-target census).
--
-- `source` follows `ref_identifiers.source` / `paper_authors.source`
-- (0168) convention: a small closed set, CHECK-enforced — a third
-- source (Semantic Scholar, …) arrives via a new forward migration
-- extending the list.
--
-- The partial index serves the stub trickle's "unheld OpenAlex targets
-- by in-corpus cite count" grouping without scanning parsed rows.
--
-- Forward-only (ADR 0005). Idempotent (`IF NOT EXISTS`). Regenerate the
-- baseline snapshot at release time (ADR 0031): `scripts/bump` /
-- `precis db dump-schema`.

BEGIN;

ALTER TABLE paper_bib_entries
    ADD COLUMN IF NOT EXISTS source      text NOT NULL DEFAULT 'bib_parse',
    ADD COLUMN IF NOT EXISTS openalex_id text;

-- ADD CONSTRAINT has no IF NOT EXISTS form; drop-then-add is the
-- repo's idempotent idiom (0201, 0207, 0224).
-- squawk-ignore ban-drop-constraint
ALTER TABLE paper_bib_entries DROP CONSTRAINT IF EXISTS paper_bib_entries_source_check;
ALTER TABLE paper_bib_entries
    ADD CONSTRAINT paper_bib_entries_source_check
    CHECK (source = ANY (ARRAY['bib_parse', 'openalex'])) NOT VALID;

-- The slice-3 stub trickle's census: unheld OpenAlex-sourced targets
-- grouped by in-corpus cite count (plus per-entry w-id lookup).
CREATE INDEX IF NOT EXISTS paper_bib_entries_openalex_id_idx
    ON paper_bib_entries (openalex_id)
    WHERE openalex_id IS NOT NULL;

COMMIT;

-- Validate outside the DDL transaction (squawk constraint-missing-not-valid);
-- every existing row has the 'bib_parse' default, so this scan cannot fail.
ALTER TABLE paper_bib_entries
    VALIDATE CONSTRAINT paper_bib_entries_source_check;

-- End of 0241_paper_bib_entries_oa_source.sql
