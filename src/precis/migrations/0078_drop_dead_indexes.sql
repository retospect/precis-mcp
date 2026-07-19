-- Drop three secondary indexes that pg_stat_user_indexes shows as idx_scan=0
-- over the DB's entire lifetime (stats_reset IS NULL on prod), i.e. never used
-- by any query plan — pure write-amplification + disk (~193 MB in prod). Each
-- was verified against every query that touches its column(s):
--
--  * chunks_dream_score_idx / chunks_watch_score_idx — bare-expression btrees on
--    (last_seen - last_dreamt) / (last_seen - last_watched). The only ORDER BY
--    over these (select_salient, _blocks_ops.py) sorts on (last_seen - col) + a
--    CASE boost term that is ALWAYS present in the SQL, so the sort key can never
--    match the bare-expression index; and it's a join+filter+sort anyway. Live
--    path, but the planner provably never uses these.
--  * llm_call_log_source_ts_idx — (source, ts) composite. No query issues a
--    sargable source= predicate: spend_rollup uses the non-sargable
--    `source IS NULL OR source = $1`, others only GROUP BY source. The ts-only
--    index (llm_call_log_ts_idx) serves the real ts-range access path.
--
-- Reversible: if a future query needs one, re-add it in a forward migration
-- (they rebuild in seconds at current table sizes).
DROP INDEX IF EXISTS chunks_dream_score_idx;
DROP INDEX IF EXISTS chunks_watch_score_idx;
DROP INDEX IF EXISTS llm_call_log_source_ts_idx;
