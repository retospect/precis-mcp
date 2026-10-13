-- 0208_llm_call_log_hash_indexes.sql
--
-- Re-create the llm_call_log hash-column b-trees that 0200 dropped (gr482932).
--
-- 0200 dropped them on idx_scan=0, but zero scans were an artifact of the
-- consumer's shape, not disuse: route_log.gc()'s orphan-blob sweep anti-joined
-- on `l.request_hash = b.hash OR l.response_hash = b.hash`, and an OR'd join
-- filter is unsargable — the planner nested-looped a full seq scan of
-- llm_call_log per llm_blob row whether or not the indexes existed (cost
-- ~2.2e10 at 11.5M rows / 4.8 GB → statement_timeout on every sweeper pass
-- once the 90-day retention first fired, 2026-10-12). The second consumer
-- never appeared in pg_stat at all: the llm_call_log_{request,response}_
-- hash_fkey RI probes fire per llm_blob DELETE — unindexed, each batch of the
-- sweep would seq-scan the whole call log.
--
-- The accompanying gc() rewrite splits the OR into two NOT EXISTS probes, so
-- each side resolves through its own index here; the RI checks use them too.
-- Plain CREATE INDEX: Migrator.apply_all() wraps each file in a transaction
-- (CONCURRENTLY cannot run there), and migrations apply in the deploy's
-- single-writer window — ~11.5M rows of 64-char hashes, same precedent as
-- 0204's index build on this table.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE INDEX IF NOT EXISTS llm_call_log_request_hash_idx
    ON llm_call_log (request_hash);
CREATE INDEX IF NOT EXISTS llm_call_log_response_hash_idx
    ON llm_call_log (response_hash);

COMMIT;
