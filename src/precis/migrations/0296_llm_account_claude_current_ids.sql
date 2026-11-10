-- 0296_llm_account_claude_current_ids.sql
--
-- The claude-sub account row (seeded by 0280) still names the retired
-- Claude ids: models {claude-haiku-4-5-20251001, claude-sonnet-5,
-- claude-opus-4-8} and default_model claude-sonnet-5, so an unpinned
-- run_slice on the lane would launch a retired model. The router's tier
-- pins (utils/llm/router.py::_TIER_MODEL) moved to the current ids in
-- the same change; this moves the row to match:
--   claude-haiku-4-5-20251001 → claude-haiku-5-5
--   claude-sonnet-5           → claude-sonnet-5-5
--   claude-opus-4-8           → claude-opus-5-5
--
-- array_replace swaps only the retired entries, so an owner's added
-- model survives; default_model moves only while it still names the
-- retired sonnet id.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- at release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

UPDATE llm_account
SET models = array_replace(
        array_replace(
            array_replace(models, 'claude-haiku-4-5-20251001', 'claude-haiku-5-5'),
            'claude-sonnet-5', 'claude-sonnet-5-5'),
        'claude-opus-4-8', 'claude-opus-5-5')
WHERE name = 'claude-sub'
  AND models && ARRAY['claude-haiku-4-5-20251001', 'claude-sonnet-5', 'claude-opus-4-8'];

UPDATE llm_account SET default_model = 'claude-sonnet-5-5'
WHERE name = 'claude-sub' AND default_model = 'claude-sonnet-5';

COMMIT;

-- End of 0296_llm_account_claude_current_ids.sql
