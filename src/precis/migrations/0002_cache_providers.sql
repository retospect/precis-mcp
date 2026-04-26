-- ===========================================================================
-- precis v2 — migration 0002: cache providers for phase 4
-- ===========================================================================
--
-- Phase 4 introduces three cache-backed kinds (math, youtube, web). The
-- cache_state schema landed in 0001; this migration only fills in the
-- providers vocabulary that those kinds need.
--
-- 'web' is generic page-fetch (httpx + trafilatura). The four other
-- web-tool providers ('perplexity', 'wolfram', 'youtube') already exist
-- from 0001.
--
-- Idempotent: ON CONFLICT DO NOTHING so re-runs are safe even though
-- the migration runner already guards against re-applying sealed rows.
-- ===========================================================================

INSERT INTO providers (slug, description) VALUES
    ('web', 'Generic web page fetch (httpx + trafilatura)')
ON CONFLICT (slug) DO NOTHING;
