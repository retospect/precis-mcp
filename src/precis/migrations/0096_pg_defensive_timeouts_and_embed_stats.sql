-- 0096_pg_defensive_timeouts_and_embed_stats.sql
--
-- chunk_embeddings statistics repair (from the 2026-07-30 schema+operation
-- review).
--
--   chunk_embeddings holds ~2.3M rows but has NEVER been ANALYZE'd
--   (pg_stat: analyze_count = 0, last_analyze IS NULL). `reltuples` is correct
--   (VACUUM maintains it) yet pg_statistic carries NO column histograms, so the
--   planner falls back to hard-coded default selectivity for every predicate on
--   the table. The KeyBERT keyword-claim query in
--   `precis.workers.chunk_keywords` LEFT JOINs chunk_embeddings and filters on
--   ce.embedder / ce.status / ce.vector IS NOT NULL / ce.content_sha — and was
--   the single heaviest statement in prod (~4.6s mean × ~50k calls ≈ 31% of all
--   DB exec time). Autoanalyze will not self-heal soon: n_mod_since_analyze
--   (~9k) sits below the ~23k threshold (50 + 0.01 × reltuples). A one-time
--   ANALYZE computes the missing stats now; the column distribution is stable
--   (embedder is always bge-m3, status mostly 'ok'), so the existing 0.01
--   autoanalyze scale factor keeps them fresh from here.
--
-- The companion defensive per-role timeouts (statement_timeout /
-- idle_in_transaction_session_timeout / lock_timeout on agent_rw / agent_ro)
-- deliberately do NOT live here. Migrations connect as agent_rw, a plain LOGIN
-- role with no CREATEROLE, so `ALTER ROLE agent_ro SET …` from a migration
-- raises `permission denied to alter role` and aborts the deploy. They are
-- applied by the postgres ansible role instead (run as the DB superuser), which
-- is also their natural home — role config, re-asserted every deploy, immune to
-- a baseline-snapshot fold.
--
-- Forward-only (ADR 0005). Idempotent (ANALYZE is re-runnable).

BEGIN;

ANALYZE chunk_embeddings;

COMMIT;

-- End of 0096_pg_defensive_timeouts_and_embed_stats.sql
