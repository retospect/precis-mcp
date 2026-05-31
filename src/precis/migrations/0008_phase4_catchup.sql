-- ===========================================================================
-- 0008_phase4_catchup.sql — materialise the phase-4 edits made directly
-- to 0001_initial.sql after it had already been applied to running DBs.
--
-- Background
-- ----------
-- Commit f27458d ("store/v2 phase 4: links + cache + schema completions",
-- 2026-05-28) edited the sealed 0001_initial.sql to add:
--
--   * 7 new rows in `relations` (derived-from, derived-into, supports,
--     supported-by, generalises, specialises, see-also)
--   * 1 new row in `providers` ('web' — direct trafilatura fetch)
--   * a whole new `cache_state` table + two indexes
--
-- DBs that were greenfield-migrated AFTER that commit get those changes
-- the first time they run 0001. DBs migrated BEFORE that commit (hephaestus
-- prod, every existing dev host) never received them — the schema seal
-- check refused to re-run an "already-applied" file, and the workaround
-- was to bump the recorded checksum without materialising the diff.
--
-- Net result: existing DBs have a 0001 row in `_migrations` with the
-- new checksum, but the new SQL never ran. Cache-backed handlers
-- (web / youtube / perplexity) crash on `cache_state` cold-path; link
-- writes for the new relation slugs raise foreign-key errors.
--
-- This migration is idempotent — every statement uses IF NOT EXISTS or
-- ON CONFLICT DO NOTHING so it's a no-op on freshly-migrated DBs where
-- 0001 already carried the phase-4 content. Going forward, never edit
-- a sealed migration; add a numbered follow-up like this one.
-- ===========================================================================


-- 1. New relation vocabulary (phase-7 link CRUD additions).
--    See LinksMixin.links_for for the inverse-slug rewrite. `see-also`
--    is asymmetric without an inverse — a one-way "for context" pointer.
INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('derived-from',    FALSE, 'derived-into', 'Source is derived from target (cause/origin)'),
    ('derived-into',    FALSE, 'derived-from', 'Source is the origin from which target derives'),
    ('supports',        FALSE, 'supported-by', 'Source provides evidence for target'),
    ('supported-by',    FALSE, 'supports',     'Source is supported by target'),
    ('generalises',     FALSE, 'specialises',  'Source is a generalisation of target'),
    ('specialises',     FALSE, 'generalises',  'Source is a specialisation of target'),
    ('see-also',        FALSE, NULL,           'One-way "for context" pointer (no inverse)')
ON CONFLICT (slug) DO NOTHING;


-- 2. `web` provider — direct trafilatura fetch via WebHandler.
INSERT INTO providers (slug, description) VALUES
    ('web', 'Direct web fetch / trafilatura extraction')
ON CONFLICT (slug) DO NOTHING;


-- 3. `cache_state` — one row per cached ref for paid-tool / web caches.
--    Lookups by (provider, request_hash); freshness derived at read time
--    from fresh_until vs now(). fresh_until = NULL pins the row (never
--    expires). Cascades on the ref_id FK so cache rows go with their ref.
CREATE TABLE IF NOT EXISTS cache_state (
    ref_id        BIGINT PRIMARY KEY REFERENCES refs(ref_id) ON DELETE CASCADE,
    provider      TEXT        NOT NULL REFERENCES providers(slug) ON UPDATE CASCADE,
    request_hash  TEXT        NOT NULL,
    model         TEXT,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    fresh_until   TIMESTAMPTZ,
    cost_usd      NUMERIC,
    meta          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (provider, request_hash)
);
CREATE INDEX IF NOT EXISTS cache_state_provider_idx
    ON cache_state (provider);
CREATE INDEX IF NOT EXISTS cache_state_fresh_until_idx
    ON cache_state (fresh_until) WHERE fresh_until IS NOT NULL;
