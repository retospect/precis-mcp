-- 0200_drop_llm_call_log_hash_indexes.sql
--
-- Drop the two llm_call_log hash indexes requested in gr461596 (~228 MB).
-- route_log.gc() still compares both hashes, and the foreign keys remain.
-- Zero observed scans do not establish that these paths have no consumers;
-- GC and foreign-key checks may scan llm_call_log after this change.
--
-- Migrator.apply_all() wraps each migration in conn.transaction(), so
-- DROP INDEX CONCURRENTLY is not supported. Use transactional drops.
-- Forward-only (ADR 0005). Idempotent. Requires the full /go gate.

DROP INDEX IF EXISTS public.llm_call_log_request_hash_idx;
DROP INDEX IF EXISTS public.llm_call_log_response_hash_idx;
