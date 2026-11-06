-- 0283_llm_account_lane_rank.sql
--
-- td568401 S1c: the builder-lane table moves off code — S1c derives
-- src/precis/plans/lanes.py's table from llm_account rows, so the order
-- and the devin seed need to live here. ``lane_rank`` carries the
-- preference order the LANES tuple held by position: ``list_accounts``
-- is name-ordered, and the rank order is what pick_lane's ties and the
-- retry ladder climb.
--
-- The devin row is brought to the LANES seed's shape. The 0280 seed
-- carried the account facts (enabled=false while the slice-host CLI
-- login was unproven, concurrency 1, all six SWE-2 ids including the
-- Flash tiers); LANES devin is enabled at cap 3 with the three SWE-2
-- tiers only — deriving as-seeded would switch the lane off and add
-- flash rungs to the ladder. The guarded UPDATE leaves an owner's
-- intervening edit alone (enabled or concurrency already changed), so
-- idempotent re-runs and prod-with-edits both converge.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE llm_account ADD COLUMN IF NOT EXISTS lane_rank integer NOT NULL DEFAULT 100;

UPDATE llm_account SET lane_rank = 10 WHERE name = 'devin'      AND lane_rank = 100;
UPDATE llm_account SET lane_rank = 20 WHERE name = 'codex'      AND lane_rank = 100;
UPDATE llm_account SET lane_rank = 30 WHERE name = 'claude-sub' AND lane_rank = 100;
UPDATE llm_account SET lane_rank = 40 WHERE name = 'kimi'       AND lane_rank = 100;
UPDATE llm_account SET lane_rank = 50 WHERE name = 'metered'    AND lane_rank = 100;

UPDATE llm_account SET enabled = true, concurrency = 3,
    models = ARRAY['swe-2-medium', 'swe-2-high', 'swe-2-max']
WHERE name = 'devin' AND enabled = false AND concurrency = 1;

COMMIT;

-- End of 0283_llm_account_lane_rank.sql
