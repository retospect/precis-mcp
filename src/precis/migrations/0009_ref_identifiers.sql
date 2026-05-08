-- Phase 9 — `ref_identifiers` table for cross-scheme alias lookup.
--
-- Motivation: a single paper can be referenced by many identifiers --
-- DOI (arXiv DOI form *and* journal DOI), arXiv id, Semantic Scholar
-- paperId, PubMed id, MAG id, OpenAlex id, DBLP, plus the local
-- `pdf_hash` content fingerprint. Today the ingest path reaches into
-- `refs.meta` per-key (`meta->>'doi'`, `meta->>'arxiv_id'`,
-- `meta->>'pdf_hash'`) and the agent-facing `get(kind='paper', id=...)`
-- only accepts DOI as an alternative to slug. Both surface the same
-- design gap: aliases live as scattered JSON columns instead of an
-- indexed lookup table.
--
-- Shape:
--   * One row per (scheme, value) -> ref_id mapping.
--   * `scheme` is an OPEN lowercase string ('doi', 'arxiv', 's2',
--     'pubmed', 'mag', 'openalex', 'dblp', 'corpusid', 'pdfsha256').
--     Adding a new scheme requires no migration.
--   * PRIMARY KEY (scheme, value) -- one identifier maps to exactly
--     one ref. Conflicting INSERTs surface a real-world duplicate
--     condition (two refs sharing the same DOI/arxiv/pdf_hash) and
--     should be resolved by soft-deleting the lesser ref.
--
--     Per-scheme reliability:
--       - 'doi'        : strict. Crossref/DataCite registries enforce
--                        one work per DOI. A collision is a real bug.
--       - 'arxiv'      : strict. arXiv mints one id per submission;
--                        versions share the bare id (no v-suffix here).
--       - 'pdfsha256'  : strict. SHA-256 collisions are cryptographically
--                        negligible.
--       - 's2'         : pragmatic. Semantic Scholar's `paperId` is a
--                        cluster key, not a work-id. S2 sometimes
--                        merges distinct papers under one paperId, and
--                        sometimes splits version-equivalents across
--                        multiple paperIds. We adopt S2's clustering
--                        wholesale: if S2 says these refs share a
--                        paperId, the first one wins the alias row;
--                        the rest surface in `ref_identifier_conflicts`
--                        for operator review. This is "if it's good
--                        enough for S2, it's good enough for us" with
--                        a manual escape hatch for cases where S2 is
--                        wrong. See:
--                          https://www.semanticscholar.org/faq/merge-pages
--
--     Multiple DOIs per paper IS legitimate (per Crossref best practice):
--       - arXiv preprint DOI + journal DOI (distinct citations)
--       - language translations (isTranslationOf)
--       - conference + journal versions (substantial revision)
--       - errata / corrections
--     This schema supports that natively: multiple `(scheme='doi',
--     value=...)` rows can point at the same `ref_id`. The PK only
--     enforces "one DOI -> one ref", which is the actual Crossref
--     guarantee.
--   * `source` (FK to providers.slug) records *who told us* about
--     this alias: the original ingestion path ('crossref', 's2',
--     'arxiv'), a local fingerprint computation ('local'), a manual
--     edit ('manual'), or doilist Layer B's S2 cluster expansion ('s2').
--
-- Naming: ``ref_identifiers`` rather than ``paper_identifiers`` so the
-- mechanism is reusable for future identifier-bearing kinds (e.g. ISBN
-- on `book`). All current usage is paper-scoped; handlers do the
-- ``r.kind = 'paper'`` filter at lookup time.
--
-- Backfill: deterministic via `DISTINCT ON (scheme, value) ORDER BY
-- scheme, value, ref_id` so the earliest ref id wins on conflict.
-- Surfaces (scheme, value) collisions silently -- run the
-- `ref_identifier_conflicts` view (added below) afterwards to find
-- duplicate refs that need manual merging.

CREATE TABLE ref_identifiers (
    scheme      TEXT         NOT NULL CHECK (scheme = lower(scheme) AND scheme <> ''),
    value       TEXT         NOT NULL CHECK (value  = lower(value)  AND value  <> ''),
    ref_id      BIGINT       NOT NULL REFERENCES refs(id) ON DELETE CASCADE,
    source      TEXT         NOT NULL REFERENCES providers(slug) ON UPDATE CASCADE,
    fetched_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (scheme, value)
);

CREATE INDEX ref_identifiers_ref_idx
    ON ref_identifiers (ref_id);
CREATE INDEX ref_identifiers_scheme_idx
    ON ref_identifiers (scheme);


-- Backfill from existing `refs.meta` JSON. Populates four schemes:
--   * doi         <- meta->>'doi'                (case-folded; DOIs are case-insensitive per spec)
--   * arxiv       <- meta->>'arxiv_id'           (e.g. '1705.02630')
--   * s2          <- meta->>'s2_id'              (40-char hex)
--   * pdfsha256   <- meta->>'pdf_hash'           (64-char hex content fingerprint)
--
-- Source provenance: copied from `refs.provider` (the ingest path that
-- minted the ref). `pdfsha256` is always 'local' because the hash is
-- computed locally regardless of which API supplied the metadata.
WITH all_ids AS (
    SELECT 'doi'::text       AS scheme,
           lower(meta->>'doi') AS value,
           id                  AS ref_id,
           COALESCE(provider, 'manual') AS source
    FROM   refs
    WHERE  kind = 'paper' AND deleted_at IS NULL
       AND meta->>'doi' IS NOT NULL AND meta->>'doi' <> ''

    UNION ALL

    SELECT 'arxiv'::text,
           lower(meta->>'arxiv_id'),
           id,
           COALESCE(provider, 'manual')
    FROM   refs
    WHERE  kind = 'paper' AND deleted_at IS NULL
       AND meta->>'arxiv_id' IS NOT NULL AND meta->>'arxiv_id' <> ''

    UNION ALL

    SELECT 's2'::text,
           lower(meta->>'s2_id'),
           id,
           COALESCE(provider, 'manual')
    FROM   refs
    WHERE  kind = 'paper' AND deleted_at IS NULL
       AND meta->>'s2_id' IS NOT NULL AND meta->>'s2_id' <> ''

    UNION ALL

    SELECT 'pdfsha256'::text,
           lower(meta->>'pdf_hash'),
           id,
           'local'
    FROM   refs
    WHERE  kind = 'paper' AND deleted_at IS NULL
       AND meta->>'pdf_hash' IS NOT NULL AND meta->>'pdf_hash' <> ''
),
deduped AS (
    -- Earliest ref id wins on (scheme, value) collision. The lost
    -- rows are the duplicate-ref problem: same identifier on two
    -- refs. They surface in `ref_identifier_conflicts` below.
    SELECT DISTINCT ON (scheme, value) scheme, value, ref_id, source
    FROM   all_ids
    ORDER BY scheme, value, ref_id
)
INSERT INTO ref_identifiers (scheme, value, ref_id, source)
SELECT scheme, value, ref_id, source
FROM   deduped;


-- Diagnostic view: surfaces every (scheme, value) that maps to more
-- than one ref. Empty in a clean store; non-empty rows are candidate
-- duplicates the operator should investigate before merging.
--
-- Use:
--   SELECT * FROM ref_identifier_conflicts;
--
-- Returns one row per offending alias with the winning ref_id (the
-- one persisted in `ref_identifiers`, lowest by id) and the full
-- list of refs that share the alias. Losing refs are
-- `all_ref_ids[2:]`. Resolve by soft-deleting the ref(s) that
-- shouldn't have been ingested, or by merging their block-level
-- annotations into the canonical ref first.
CREATE VIEW ref_identifier_conflicts AS
WITH all_ids AS (
    SELECT 'doi'::text      AS scheme, lower(meta->>'doi')      AS value, id AS ref_id
    FROM refs WHERE kind='paper' AND deleted_at IS NULL AND meta->>'doi'      IS NOT NULL AND meta->>'doi'      <> ''
    UNION ALL
    SELECT 'arxiv',          lower(meta->>'arxiv_id'),               id
    FROM refs WHERE kind='paper' AND deleted_at IS NULL AND meta->>'arxiv_id' IS NOT NULL AND meta->>'arxiv_id' <> ''
    UNION ALL
    SELECT 's2',             lower(meta->>'s2_id'),                  id
    FROM refs WHERE kind='paper' AND deleted_at IS NULL AND meta->>'s2_id'    IS NOT NULL AND meta->>'s2_id'    <> ''
    UNION ALL
    SELECT 'pdfsha256',      lower(meta->>'pdf_hash'),               id
    FROM refs WHERE kind='paper' AND deleted_at IS NULL AND meta->>'pdf_hash' IS NOT NULL AND meta->>'pdf_hash' <> ''
)
SELECT scheme,
       value,
       MIN(ref_id)                       AS winning_ref_id,
       array_agg(ref_id ORDER BY ref_id) AS all_ref_ids,
       count(*)                          AS ref_count
FROM   all_ids
GROUP  BY scheme, value
HAVING count(*) > 1;
