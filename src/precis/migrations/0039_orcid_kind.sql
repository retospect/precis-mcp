-- 0039_orcid_kind.sql
--
-- ADR 0039 — register `kind='orcid'`: a stored, refreshable author node.
--
-- An ORCID record is a first-class author identity: a durable `refs` row
-- (kind='orcid'), slug `orcid:<iD>` (e.g. orcid:0000-0002-1825-0097), whose
-- `meta` holds the structured record (names, biography, keywords,
-- employments with ROR ids, work-count, fetched_at). A single
-- `card_combined` chunk (name + bio + keywords + affiliations) is embedded
-- so authors are semantically searchable ("the corpus's spintronics PIs").
--
-- Unlike web/wikipedia/youtube/semanticscholar, `orcid` is NOT a
-- cache-backed kind: the node is a *link hub* (authorship edges) and cache
-- eviction must never drop its edges. It therefore reuses the shared
-- `refs` + `chunks` columns directly (like `paper`), not `cache_state`.
--
-- This migration seeds two registry rows (cf. 0026_wikipedia_kind.sql):
--   * `kinds.orcid`     — runtime kind validator (SELECT slug FROM kinds)
--   * `providers.orcid` — refs.provider FK target (the durable refs the
--                          handler mints stamp provider='orcid')
-- Without the providers row, the first ref write trips refs_provider_fkey
-- (cf. 0012_epo_ops_provider.sql).
--
-- The `authored` / `authored-by` link relations land in the companion
-- 0039_authored_relation.sql.
--
-- Forward-only (ADR 0005). Idempotent under ON CONFLICT DO NOTHING.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('orcid', FALSE, 'ORCID author',
     'A researcher identity resolved from ORCID (https://orcid.org). '
     'Slug-addressed by iD (e.g. ''orcid:0000-0002-1825-0097''). '
     'get resolves + stores the record (names, bio, keywords, '
     'employments with ROR ids), links works already held, and reports '
     'the missing ones — fetching them is LLM-gated via '
     'args={''enqueue'': N}; search runs over the embedded author card; '
     'link/tag attach authorship edges (authored / authored-by) and '
     'classification. Durable link hub — never cache-evicted. See '
     '``precis-orcid-help``.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO providers (slug, description) VALUES
    ('orcid', 'ORCID Public API (https://pub.orcid.org/v3.0/) — author identity + works')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0039_orcid_kind.sql
