-- 0112_llm_call_log_placement.sql
--
-- Record WHERE a call ran, so the dollar caps can stop counting local time as
-- money.
--
-- `llm_call_log.cost_usd` mixes two different things. A claude/OpenRouter row
-- is real money (or real subscription quota). A row served by the cluster's own
-- GPUs is a *priced* figure from `precis.budget.pricing` — an estimate of what
-- the same tokens would have cost elsewhere. Nothing left an account.
--
-- Until now nothing distinguished them: `transport='openai_tools'` covers BOTH
-- the local qwen3-235b rung and the cloud glm-5.2 rung of `llm.chain.big`, so
-- the only discriminator was the model string, which is not a contract.
--
-- Why it matters. `PRECIS_DAILY_COST_CEILING` sums `cost_usd` across every row,
-- so as passes move onto local hardware the ceiling keeps filling up at ~$0.35 a
-- planner tick — and when it trips it stops the LOCAL work too. That inverts the
-- intent: the machines are a sunk cost, so an idle GPU is the waste, and a
-- budget gate that idles them is doing harm rather than nothing. (The ADR 0066
-- §5 breaker already got this right — `_rung_is_cloud` keeps a tripped $ cap
-- from starving a local rung; the planner guardrails simply never learned the
-- distinction.)
--
-- The column is written from the rung that ACTUALLY ran, not the one the
-- operator configured: a local primary that fell back to a cloud rung really did
-- spend money and must still count.
--
-- Nullable, and NULL is read as cloud (fail-closed) — every pre-existing row is
-- unclassified, and an old row is far likelier to be a billed claude call than a
-- free local one. Deliberately NOT backfilled by model name: `qwen3-235b` is
-- local *today*, but encoding that guess as data would make a wrong exclusion
-- permanent and invisible. The caps simply get more accurate as new rows land.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS placement text;

COMMENT ON COLUMN llm_call_log.placement IS
    'local | cloud for the rung that ran (router._placement_of). Local rows '
    'carry a PRICED cost_usd, not money spent — the planner dollar caps '
    'exclude them. NULL (pre-0112) is treated as cloud.';

-- The caps' hot query is "sum cost_usd over the trailing 24h, excluding local".
-- Partial on the exclusion so the index stays small and serves exactly it; the
-- existing ts index still covers the unfiltered mining queries.
CREATE INDEX IF NOT EXISTS llm_call_log_billable_ts_idx
    ON llm_call_log (ts DESC)
    WHERE cost_usd IS NOT NULL AND placement IS DISTINCT FROM 'local';

COMMIT;
