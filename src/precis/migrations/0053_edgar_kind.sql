-- 0053_edgar_kind.sql
--
-- Register `kind='edgar'` for read-only SEC EDGAR filings.
--
-- Motivation: company disclosure (SEC filings) is the third public-
-- record corpus alongside `paper` (academic) and `patent` (EPO OPS).
-- An `edgar` ref is one filing (10-K / 10-Q / 8-K / S-1 / …),
-- accession-slugged (`0000320193-23-000106`), fetched-as-ingest from
-- the key-less SEC APIs. Search merges local + EDGAR full-text.
--
-- Schema additions are data-only — every column `edgar` uses already
-- exists on the shared `refs` + `chunks` tables. This migration seeds:
--   * the kind registry row (also boot-upserted from KindSpec, but a
--     fresh DB needs it before first boot for the refs.kind FK);
--   * one new chunk_kind `edgar_section` (one chunk per filing
--     paragraph, labelled with its 10-K Item / 8-K item code via
--     chunks.section_path + meta.item_code);
--   * the `sec_edgar` provider (refs.provider FK target) and a
--     `sec_edgar_search` provider reserved for the full-text search
--     cache leg.
--
-- Forward-only (ADR 0005). Statements are idempotent under
-- `ON CONFLICT DO NOTHING` so a re-run after a partial apply is safe.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('edgar', FALSE, 'SEC Filing',
     'Read-only SEC EDGAR filing (10-K / 10-Q / 8-K / S-1 / …). '
     'Accession-slugged (e.g. 0000320193-23-000106). Search merges '
     'local + EDGAR full-text; get(id=...) fetches the submissions '
     'index + primary document and stores section-labelled blocks. '
     'get(id=''cik:320193'' | ''ticker:aapl'') lists a company''s recent '
     'filings; view=''diff'' shows quarter-to-quarter section changes. '
     'See ``precis-edgar-help``.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('edgar_section', FALSE,
     'One paragraph/section block of an SEC filing, labelled with its '
     'standard section via chunks.section_path + meta.item_code '
     '(e.g. Item 1A Risk Factors, 8-K Item 2.02). Distinct from '
     '``paragraph`` so section-scoped search and the quarter-to-quarter '
     'diff can align the same section across consecutive filings.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO providers (slug, description) VALUES
    ('sec_edgar', 'US SEC EDGAR — company filings (submissions + archive APIs)'),
    ('sec_edgar_search', 'US SEC EDGAR — full-text search (efts.sec.gov)')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0053_edgar_kind.sql
