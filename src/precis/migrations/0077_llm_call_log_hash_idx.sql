-- Index the llm_call_log hash columns the orphan-blob GC anti-joins on.
--
-- route_log.gc() runs
--   DELETE FROM llm_blob b WHERE NOT EXISTS (
--     SELECT 1 FROM llm_call_log l
--      WHERE l.request_hash = b.hash OR l.response_hash = b.hash)
-- and llm_call_log had indexes only on (id) and (ts). With neither hash column
-- indexed, that anti-join sequential-scanned the whole call log per blob row —
-- 30-60s of pure CPU per pass. Run every sweeper cycle across the fleet with no
-- single-flight guard, it kept the DB host (caspar) saturated. A plain b-tree on
-- each column lets the planner do a bitmap-OR anti-join (ms, not minutes).
--
-- 0077's companion route_log.gc() change adds the advisory-lock single-flight and
-- skips the sweep on no-op passes; this index makes it fast when it does run.
--
-- 140k rows / ~89 MB in prod → sub-second plain build, so no CONCURRENTLY.
CREATE INDEX IF NOT EXISTS llm_call_log_request_hash_idx
    ON llm_call_log (request_hash);
CREATE INDEX IF NOT EXISTS llm_call_log_response_hash_idx
    ON llm_call_log (response_hash);
