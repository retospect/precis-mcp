-- Index ref_events for the "last manual-open attempt per ref" lookup the
-- Drive downloads queue's untried-first sort runs (routes/drive.py,
-- store.recent_refs(untried=True)):
--
--   LEFT JOIN (
--     SELECT ref_id, MAX(ts) AS last_tried FROM ref_events
--      WHERE source = 'manual:open' GROUP BY ref_id
--   ) mt ON mt.ref_id = r.ref_id
--
-- Neither existing index covers this: ref_events_ref_id_ts_idx is
-- (ref_id, ts DESC) with no source column, and
-- ref_events_source_event_ts_idx is (source, event, ts DESC) with no
-- ref_id. This one lets the aggregate resolve as an index-only scan
-- instead of a heap-touching filter over the whole table.
--
-- 253k rows / ~95 MB in prod as of 2026-08 (same order of magnitude as
-- 0077's llm_call_log hash indexes, which built sub-second plain) → no
-- CONCURRENTLY needed.
--
-- Forward-only (ADR 0005). IF NOT EXISTS makes a re-run after a partial
-- apply safe.

CREATE INDEX IF NOT EXISTS ref_events_ref_id_source_ts_idx
    ON ref_events (ref_id, source, ts);
