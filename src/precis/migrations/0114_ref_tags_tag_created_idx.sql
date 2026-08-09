-- 0114_ref_tags_tag_created_idx.sql
--
-- Index ref_tags for the Now tab's "recent terminal jobs" query
-- (precis_web/routes/status.py::_now_jobs) — one of the sub-tab's helpers
-- polled every 10s:
--
--   SELECT r.ref_id, r.title, t.value, rt.created_at
--     FROM refs r
--     JOIN ref_tags rt ON rt.ref_id = r.ref_id
--     JOIN tags t ON t.tag_id = rt.tag_id
--    WHERE r.kind = 'job' AND r.deleted_at IS NULL
--      AND t.namespace = 'STATUS' AND t.value = ANY(terminal_values)
--    ORDER BY rt.created_at DESC
--    LIMIT 20
--
-- The only existing index on ref_tags is ref_tags_tag_id_idx (tag_id) —
-- it narrows to the TERMINAL status tag_ids but then has to fetch and sort
-- every historical terminal ref_tags row to satisfy the ORDER BY ... LIMIT.
-- Adding created_at to the index lets that resolve as a backward index scan
-- that stops after 20 rows instead of a fetch-all-then-sort of the whole
-- terminal history.
--
-- Forward-only (ADR 0005). IF NOT EXISTS makes a re-run after a partial
-- apply safe.

CREATE INDEX IF NOT EXISTS ref_tags_tag_id_created_at_idx
    ON ref_tags (tag_id, created_at DESC);

-- End of 0114_ref_tags_tag_created_idx.sql
