-- 0355_cache_result_versions.sql
-- Historical results retain their request hash and their own address.
ALTER TABLE cache_state
    ADD COLUMN IF NOT EXISTS version_root bigint,
    ADD COLUMN IF NOT EXISTS is_current boolean NOT NULL DEFAULT true;

CREATE UNIQUE INDEX IF NOT EXISTS cache_state_current_request_idx
    ON cache_state (provider, request_hash) WHERE is_current;

CREATE UNIQUE INDEX IF NOT EXISTS cache_state_current_version_idx
    ON cache_state ((COALESCE(version_root, ref_id))) WHERE is_current;

-- The partial index above retains this guarantee for the current result.
-- squawk-ignore ban-drop-constraint
ALTER TABLE cache_state DROP CONSTRAINT IF EXISTS cache_state_provider_request_hash_key;

-- Pin existing rows on the former default TTL. Preserve non-default TTLs
-- and rows intentionally born stale.
UPDATE cache_state cs
SET fresh_until = NULL
FROM refs r
WHERE r.ref_id = cs.ref_id AND r.kind = 'websearch'
  AND cs.fresh_until - cs.fetched_at = interval '7 days';
