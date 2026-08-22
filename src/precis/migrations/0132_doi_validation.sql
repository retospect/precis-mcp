-- 0132_doi_validation.sql
--
-- DOI-validity signal for the retraction walk (docs/backlog/
-- draft-doi-completeness-check.md). DOI *presence* is a pure read off
-- `ref_identifiers` (no schema change needed — a `ref` already carries a
-- `doi` alias there beside `retraction_status`). DOI *validity* — does the
-- DOI actually resolve upstream (Crossref) — needs its own stamp, mirroring
-- `retraction_status` / `retraction_checked_at` exactly:
--
--   doi_status        -- 'valid' | 'not_found', NULL = never validated
--   doi_validated_at  -- when we last asked upstream, NULL = never
--
-- `doi_validated_at IS NULL` is the "never validated" first-class state the
-- backlog item calls out (distinct from "validated and fine" — never rounded
-- down to clean, mirroring `check_ref_retraction`'s never-checked/known-clean
-- split). Once validated, `doi_status` is always written (`'valid'` or
-- `'not_found'`) alongside the stamp — unlike `retraction_status`, there is
-- no second source (no Retraction-Watch equivalent) that could disagree with
-- a clean Crossref read, so there is nothing to preserve by leaving the
-- column NULL on success.
--
-- Additive + nullable (ADR 0005 forward-only). No backfill: every existing
-- ref reads as "never validated", which is the correct first-class state
-- for a DOI nobody has ever asked Crossref about.
-- Regenerate the baseline snapshot after merge (ADR 0031): scripts/bump.

BEGIN;

ALTER TABLE refs ADD COLUMN IF NOT EXISTS doi_status text;
ALTER TABLE refs ADD COLUMN IF NOT EXISTS doi_validated_at timestamptz;

ALTER TABLE refs DROP CONSTRAINT IF EXISTS refs_doi_status_check;
ALTER TABLE refs ADD CONSTRAINT refs_doi_status_check
    CHECK (doi_status IS NULL OR doi_status = ANY (ARRAY['valid'::text, 'not_found'::text]));

COMMIT;

-- End of 0132_doi_validation.sql
