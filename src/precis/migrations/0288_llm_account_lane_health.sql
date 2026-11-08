-- 0288_llm_account_lane_health.sql
--
-- gr588221: lane health lives on the lane's own account row. The 2026-11-08
-- 03:13Z incident: claude-sub's `claude -p` answered "Not logged in · Please
-- run /login" and every session on the lane failed until a human flipped
-- `enabled` by hand. These columns let the lanes self-heal instead:
--
--   parked_reason      text — NULL = healthy; a short "class: key" mark
--                      ("auth:not-logged-in", "outage:timeout") while the
--                      lane is held out of dispatch. Parked lanes are
--                      skipped exactly like enabled=false, but `enabled`
--                      stays the HUMAN switch — nothing here writes it.
--   parked_at          timestamptz — first sighting of the current park
--                      (COALESCE on re-park so a flap does not extend the
--                      >6h alert clock).
--   health_checked_at  timestamptz — the last lane_health probe pass.
--   health_outcome     text — that pass's short code:
--                      ok | auth | outage | unprobed.
--
-- The hourly `lane_health` job probes each enabled lane and clears
-- parked_* on a green probe — the lane rejoins dispatch with no human
-- toggle. No seed rows change: every existing row is healthy (NULLs).
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE llm_account ADD COLUMN IF NOT EXISTS parked_reason text;
ALTER TABLE llm_account ADD COLUMN IF NOT EXISTS parked_at timestamptz;
ALTER TABLE llm_account ADD COLUMN IF NOT EXISTS health_checked_at timestamptz;
ALTER TABLE llm_account ADD COLUMN IF NOT EXISTS health_outcome text;

COMMIT;

-- End of 0288_llm_account_lane_health.sql
