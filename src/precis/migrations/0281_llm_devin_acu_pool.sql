-- 0281_llm_devin_acu_pool.sql
--
-- td572038 (Reto 2026-11-06 ruling): Devin sessions meter in ACUs, so the
-- ``devin`` account gets a second pool beside ``llm:devin:sessions`` —
-- ``llm:devin:acu``, weekly-resetting on the calendar rule
-- ``calendar:weekly:sun:08:00@Etc/UTC``. The ACU allotment is unknown, so
-- ``limit_reported`` stays NULL (unknown is NULL, never zero). NO dollar
-- pool for Devin — on-demand dollars stay a hard stop in job meta — and
-- NO OpenRouter management key, so ``llm:openrouter:credits`` keeps no
-- limit either: both read as cumulative spend whose pacing stays
-- ``unknown``.
--
-- Same rate-less carrier shape as the 0280 seeds so
-- rate_limit.acquire/park/_record_refusal keep working unchanged. Both
-- tables are already in SEED_TABLES; no GRANT (agent_ro is provisioned
-- out of tree).
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

INSERT INTO external_rate_limits (
    provider, capacity, refill_per_sec, tokens, daily_cap, "class", unit,
    reset_rule, limit_reported
) VALUES
    ('llm:devin:acu', 1, 1.0, 1, NULL, 'llm-api', 'acu',
     'calendar:weekly:sun:08:00@Etc/UTC', NULL)
ON CONFLICT (provider) DO NOTHING;

UPDATE llm_account SET pools = array_append(pools, 'llm:devin:acu')
WHERE name = 'devin' AND NOT ('llm:devin:acu' = ANY(pools));

COMMIT;

-- End of 0281_llm_devin_acu_pool.sql
