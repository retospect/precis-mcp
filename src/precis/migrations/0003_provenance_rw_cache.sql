-- ===========================================================================
-- 0003_provenance_rw_cache.sql — Retraction Watch dataset cache.
--
-- Phase 3 of docs/provenance-kind-plan.md. Backs the monthly sync job
-- (jobs/provenance_rw_sync.py) and the join into the provenance report.
-- Crossref's `update-to` field carries notice DOIs + types but no
-- human-readable reasons; the Retraction Watch dataset (distributed
-- under CC-BY via Crossref since Dec 2023) adds reason codes like
-- "+Falsification/Fabrication of Data" or "+Author Unresponsive".
--
-- Schema mirrors the 20-column RW CSV (see
-- gitlab.com/crossref/retraction-watch-data/blob/main/README.md):
-- Record ID, Title, Subject, Institution, Journal, Publisher,
-- Country, Author, URLS, ArticleType, RetractionDate, RetractionDOI,
-- RetractionPubMedID, OriginalPaperDate, OriginalPaperDOI,
-- OriginalPaperPubMedID, RetractionNature, Reason, Paywalled, Notes.
--
-- We materialise only the fields needed for the join + render path
-- (paper_doi, notice_doi, nature, reasons, retraction_date) plus the
-- full original row as raw JSONB for forensic reference.
-- ===========================================================================


-- Cache of RW dataset rows, keyed by RW's own Record ID so re-syncs
-- are idempotent. One paper can have multiple rows (a correction
-- then a later retraction = two records); we don't collapse them
-- because the renderer wants the full chronology.
CREATE TABLE provenance_rw_cache (
    record_id        BIGINT      PRIMARY KEY,           -- RW dataset row id
    paper_doi        TEXT        NOT NULL,              -- OriginalPaperDOI, lowercased
    notice_doi       TEXT,                              -- RetractionDOI, lowercased; may be empty
    notice_nature    TEXT        NOT NULL,              -- "Retraction" | "Correction" | "Expression of concern" | …
    reasons          TEXT[]      NOT NULL DEFAULT '{}', -- semicolon-split Reason field
    retraction_date  DATE,                              -- RetractionDate, parsed
    paper_title      TEXT,                              -- OriginalPaperTitle, for display
    journal          TEXT,
    raw              JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- full CSV row
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The join path: provenance.check_doi looks up by canonical paper_doi.
CREATE INDEX provenance_rw_paper_doi_idx
    ON provenance_rw_cache (paper_doi);

-- Optional secondary lookup: which notices retract a given DOI?
-- Useful for the Phase 4 transitive cite-walk so it doesn't have
-- to round-trip through Crossref for each cited paper.
CREATE INDEX provenance_rw_notice_doi_idx
    ON provenance_rw_cache (notice_doi)
    WHERE notice_doi IS NOT NULL;


-- Ledger of when each source URL was last synced + how many rows it
-- produced. ON CONFLICT (source_url) DO UPDATE keeps the latest
-- successful sync per source.
CREATE TABLE provenance_rw_sync (
    source_url        TEXT        PRIMARY KEY,
    last_full_sync_at TIMESTAMPTZ,
    last_row_count    INT,
    last_status       TEXT,  -- 'ok' | 'partial' | 'failed'
    last_error        TEXT   -- populated on 'partial' / 'failed'
);
