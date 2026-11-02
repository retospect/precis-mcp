-- 0272_llm_call_log_billed_usd.sql
--
-- Record what a call was actually BILLED, so the daily cost ceiling can stop
-- counting nominal subscription dollars as money spent (gr549279).
--
-- `llm_call_log.cost_usd` on transport='claude_p' is Claude Code's
-- list-price estimate (`claude -p --output-format json`'s
-- `total_cost_usd`), not money that left an account: the deploy user's
-- login is an OAuth subscription, so those calls bill $0. The ceiling
-- summed the nominal figure anyway — one two-hour taproot:dedup burst
-- ($161 nominal) tripped the $/24h cap and blacked out the nursery
-- cadences for a full day.
--
-- `billed_usd` is stamped at log time (route_log._write):
--   * transport='claude_p' + PRECIS_CLAUDE_P_BILLED falsy  -> 0
--   * everything else                                     -> cost_usd
-- The json envelope cannot say how a call was paid, so the deploy var
-- `precis_claude_p_billed` declares it per host. cost_usd stays the
-- nominal figure for cost bands / p90 estimates; billed_usd is the only
-- thing PRECIS_DAILY_COST_CEILING sums.
--
-- Nullable and NOT backfilled: the ceiling's window is a rolling 24h, so
-- readers treat NULL (every pre-0272 row) as cost_usd and correctness
-- arrives as the window rolls. A NULL placement still counts as cloud.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS billed_usd numeric;

COMMENT ON COLUMN llm_call_log.billed_usd IS
    'Dollars actually billed for the call (0272). claude_p rows stamp 0 '
    'when the worker''s claude login is a subscription '
    '(PRECIS_CLAUDE_P_BILLED falsy), else cost_usd; every other transport '
    'stamps cost_usd. The daily cost ceiling sums this column; NULL '
    '(pre-0272 row) reads as cost_usd.';

COMMIT;
