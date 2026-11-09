-- 0293_venue_metrics.sql
--
-- Journal venue metrics (gr606422): per-ISSN impact signals for surveying
-- a publication list — precis paper metadata (Semantic Scholar, Crossref,
-- DataCite, OpenAlex) carries none, so a venue standing check meant
-- scraping it off a web page. JCR is licensed and stays out; the two free
-- feeds land in one table:
--
--   * Scimago SJR yearly CSV (CC BY) — one row per (issn, year) carrying
--     ``sjr`` and ``sjr_quartile`` for that vintage;
--   * OpenAlex ``/sources`` endpoint — ``openalex_2yr_mean_citedness`` +
--     ``openalex_h_index`` snapshot rows at the fetch year (a source absent
--     from Scimago still lands a row, quartile NULL).
--
-- ``issn`` is stored NORMALIZED — uppercase, hyphens stripped
-- (``13858947``), matching
-- ``upper(regexp_replace(refs.meta->>'issn', '[^0-9Xx]', '', 'g'))`` — so a
-- paper's Crossref ISSN joins without a format column. Sources commonly
-- list two ISSNs (print + electronic); each gets its own row. The upsert
-- fills columns independently (``COALESCE(EXCLUDED.x, t.x)``) so an OA
-- refresh never nulls SJR data and vice versa.
--
-- Readers: the paper overview renders the nearest-year SJR row + latest
-- OA row; ``search(kind='paper', args={'min_quartile': 'Q1'})`` filters on
-- the venue's latest SJR quartile.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS venue_metrics (
    issn                         text NOT NULL,    -- normalized: uppercase, no hyphen
    year                         bigint NOT NULL,   -- Scimago vintage, or OA fetch year
    title                        text,              -- venue display name
    sjr                          double precision,
    sjr_quartile                 text CHECK (sjr_quartile IN ('Q1', 'Q2', 'Q3', 'Q4')),
    openalex_source_id           text,              -- 'S…' short id
    openalex_2yr_mean_citedness  double precision,
    openalex_h_index             integer,
    updated_at                   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (issn, year)
);

COMMENT ON TABLE venue_metrics IS
    'gr606422: journal venue metrics keyed by normalized ISSN (uppercase, '
    'no hyphen) + vintage year. Scimago SJR rows carry sjr/sjr_quartile; '
    'OpenAlex /sources rows carry 2yr mean citedness + h-index at the '
    'fetch year. JCR is licensed and deliberately absent.';

-- The min_quartile search filter: latest quartile per ISSN.
CREATE INDEX IF NOT EXISTS venue_metrics_quartile
    ON venue_metrics (sjr_quartile) WHERE sjr_quartile IS NOT NULL;
-- The OA refresh leg: rows still carrying OpenAlex columns.
CREATE INDEX IF NOT EXISTS venue_metrics_openalex
    ON venue_metrics (openalex_source_id) WHERE openalex_source_id IS NOT NULL;

COMMIT;

-- End of 0293_venue_metrics.sql
