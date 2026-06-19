-- 0026_wikipedia_kind.sql
--
-- Register `kind='wikipedia'` — on-demand Wikipedia article fetch.
--
-- Motivation: the on-demand alternative to bulk-embedding a Wikipedia
-- dump (~30M chunks, ~200 GB resident HNSW, a permanent precision tax
-- on every search). A `wikipedia` ref is fetched only when asked:
-- resolve a query to the best article via the MediaWiki search API,
-- fetch its plain-text extract, cache it 7 days, block-split + embed
-- via the standard pipeline. A handful of articles per fetch, TTL-
-- expired, instead of a resident dump diluting the papers corpus.
--
-- Schema additions are data-only — `wikipedia` reuses the shared
-- `refs` + `chunks` + `cache_state` columns and the default
-- `paragraph` chunk_kind. This migration seeds two registry rows:
--   * `kinds.wikipedia`     — runtime kind validator (SELECT slug FROM kinds)
--   * `providers.wikipedia` — cache_state/refs.provider FK target
-- Without the providers row, the first cache write trips
-- refs_provider_fkey (cf. 0012_epo_ops_provider.sql).
--
-- Forward-only (ADR 0005). Idempotent under ON CONFLICT DO NOTHING.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('wikipedia', FALSE, 'Wikipedia (on-demand article fetch)',
     'Resolve a query to the best-matching Wikipedia article via the '
     'MediaWiki search API, then fetch and cache its plain-text extract. '
     'Slug-addressed by query; cached 7 days; block-split + embedded so '
     'search(kind=''wikipedia'', q=...) lands hits inside fetched '
     'articles. On-demand — no bulk dump, always current. See '
     '``precis-wikipedia-help``.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO providers (slug, description) VALUES
    ('wikipedia', 'Wikipedia / MediaWiki API (search + plain-text extracts)')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0026_wikipedia_kind.sql
