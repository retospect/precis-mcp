-- 0303_tool_calls_result_ids.sql
--
-- The recall audit's missing half (gr570722, review td627401):
-- ``result_count`` records HOW MANY items a response enumerated; this
-- column records WHICH ref ids it returned, so "did that memory search
-- surface meN" answers off the ledger row itself — no body re-parsing
-- and no dependence on a get-path ``arg_sample->>'id'`` the way the
-- environment view's read windows still work. Populated from
-- ``Response.result_ids`` at the dispatch chokepoint, same posture as
-- ``result_count``: set where the ids are already in hand, never an
-- extra query for the ledger's sake.
--
-- Column:
--   * result_ids: bigint[] of ref ids the response enumerated, write-
--     capped at 256 ids (tool_ledger.RESULT_IDS_MAX); NULL on errors
--     and on responses that don't enumerate a result set.
--
-- Forward-only (ADR 0005). No backfill — pre-0304 rows keep NULL; a
-- pre-0304 audit window still parses rendered bodies or arg_sample.

BEGIN;

ALTER TABLE tool_calls
    ADD COLUMN result_ids bigint[];

COMMENT ON COLUMN tool_calls.result_ids IS
    'Ref ids the response enumerated (Response.result_ids) — the recall audit''s "which handles did this call return" half of result_count; write-capped at 256. NULL where nothing is enumerated.';

COMMIT;

-- End of 0303_tool_calls_result_ids.sql
