-- 0001_estimate_kind.sql  (plugin namespace: precis_estimate)
--
-- The `estimate` kind: a cache-backed ms chemistry-workup panel
-- (docs/backlog/estimate-kind-ms-chemistry-workup.md). `EstimateHandler`
-- subclasses `CacheBackedHandler`, which stores each panel as an ordinary
-- ref + `cache_state` row (the same shared tables `math`/`web`/`youtube`
-- use — no new table). Two FK targets still need a row apiece before that
-- path will accept `kind='estimate'` / `provider='estimate'`: `kinds` and
-- `providers`. Body chunks land under the core `'paragraph'` chunk_kind
-- (insert_blocks' default) — no chunk_kinds row needed.
--
-- Forward-only + idempotent (ADR 0005). Runs AFTER the precis-core chain
-- (the migrator orders built-ins first), so `kinds` / `providers` exist.

BEGIN;

-- 1. the provider (cache_state.provider FK) --------------------------
INSERT INTO providers (slug, description) VALUES
    ('estimate',
     'Local ms chemistry-workup panel (mendeleev + ase.data + a vendored '
     'Hammer-Norskov d-band table) - deterministic, no external network '
     'call, hypothesis-generating only')
ON CONFLICT (slug) DO NOTHING;

-- 2. the ref kind ------------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('estimate', FALSE, 'Estimate (ms chemistry workup)',
     'Millisecond semi-empirical chemistry workup - a hypothesis-generator, '
     'inadmissible for rulings (measure before citing as fact). '
     'Slug-addressed by canonicalised composition; cache-backed, '
     'deterministic, pinned TTL. Slice 1 ships the composition tier only. '
     'See precis-estimate-help.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0001_estimate_kind.sql
