-- 0280_llm_account_pools.sql
--
-- S1a (memory me568172/me568184, ruling me568199): one ledger for owned
-- LLM capacity. The bookable unit is an offering (model x variant or
-- effort x provider x account x access method); the card
-- (precis.llm_catalog), its offerings and its endpoints already cover
-- three of the five records. This migration adds the other two:
--
--   llm_account   — one credential at one provider under one plan.
--                    Not an llm.profiles row: a profile carries base_url
--                    + wire settings for an HTTP transport; an account
--                    needs adapter, concurrency, task classes, models,
--                    entitlement and privacy evidence that profiles have
--                    no room for. api_key_ref is a bare secret NAME
--                    resolved by precis.secrets.get_secret, never a
--                    value or a base_url — the account put (S1a step 4)
--                    is owner-only precisely so an agent-writable row
--                    can never route a vault secret to an arbitrary
--                    host. Seeded from today's lanes
--                    (src/precis/plans/lanes.py::LANES); LANES itself is
--                    unchanged here (S1c derives it from this table).
--
--   external_rate_limits (0121/0233) gains nullable reporting + reset
--   columns so a pool (a rate bucket with a reset rule and explicit
--   scope) can carry a provider-reported snapshot alongside the
--   existing token-bucket carrier shape. ``provider`` stays the primary
--   key and the pool key (grammar ``llm:<account>:<window>``); daily_cap
--   / day_used are not reused for pool reporting — unknown is NULL,
--   never zero. Reset-rule grammar: ``rolling:<seconds>``;
--   ``calendar:<daily|weekly|monthly>:<anchor>@<IANA tz>``;
--   ``expires:<ISO UTC>``.
--
-- Six pool rows are seeded in the rate-less carrier shape (capacity 1,
-- refill_per_sec 1.0, tokens 1, class llm-api, daily_cap NULL) so
-- rate_limit.acquire/park/_record_refusal keep working unchanged — no
-- routing behaviour changes in this slice.
--
-- OUT OF SCOPE here: deriving LANES from llm_account (S1c); SecretSpecs
-- for OPENROUTER_API_KEY/DEEPINFRA_API_KEY (S1b); pacing (S3, the
-- reported_* columns are read-only snapshots until then); signal
-- harvesting (S2); resolve_profile / the llm.profiles grammar (untouched).
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS llm_account (
    name            text PRIMARY KEY,
    vendor          text NOT NULL,
    plan            text NOT NULL,
    adapter         text NOT NULL CHECK (adapter IN (
                        'claude_p', 'claude_agent', 'codex_exec',
                        'devin_tree', 'devin_api', 'kimi_tree', 'api',
                        'openai_compat', 'openai_tools', 'local')),
    -- The llm.profiles entry carrying this account's base_url + wire
    -- settings, for an HTTP account. base_url never lives on this row.
    profile         text,
    -- A bare secret NAME (precis.secrets.get_secret / KNOWN_SECRETS),
    -- never a value. NULL = no vault credential (a per-host OAuth/CLI
    -- login instead — claude-sub, codex today).
    api_key_ref     text,
    concurrency     integer NOT NULL DEFAULT 1,
    task_classes    text[] NOT NULL DEFAULT '{}',
    models          text[] NOT NULL DEFAULT '{}',
    default_model   text,
    enabled         boolean NOT NULL DEFAULT false,
    -- Allowed model ids with account-effective price, source and date.
    -- Open-ended per account; see tests/test_schema_design.py JSONB_COLUMNS.
    entitlement     jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- training_use / retention / locality, each with a source URL and
    -- verified_at. Open-ended per account; see JSONB_COLUMNS.
    privacy         jsonb NOT NULL DEFAULT '{}'::jsonb,
    promo_expiry    date,
    -- Pool keys (external_rate_limits.provider) this account draws from.
    pools           text[] NOT NULL DEFAULT '{}',
    updated_at      timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS unit text
    CHECK (unit IN ('calls', 'tokens', 'usd', 'acu', 'sessions', 'percent'));
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS limit_reported     numeric;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS used_reported      numeric;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS remaining_reported numeric;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS reported_at        timestamptz;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS source text
    CHECK (source IN ('provider', 'estimate', 'user'));
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS confidence         text;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS reset_rule         text;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS resets_at          timestamptz;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS expires_at         timestamptz;

-- Account seeds, mirroring src/precis/plans/lanes.py::LANES today.
INSERT INTO llm_account (
    name, vendor, plan, adapter, api_key_ref, concurrency, task_classes,
    models, default_model, enabled, entitlement, promo_expiry, pools
) VALUES
    ('claude-sub', 'anthropic', 'subscription', 'claude_p', NULL, 2,
     ARRAY['*'],
     ARRAY['claude-haiku-4-5-20251001', 'claude-sonnet-5', 'claude-opus-4-8'],
     'claude-sonnet-5', true, '{}'::jsonb, NULL,
     ARRAY['llm:claude-sub:5h', 'llm:claude-sub:7d']),
    ('codex', 'openai', 'subscription', 'codex_exec', NULL, 1,
     ARRAY['code', 'review', 'validate'], ARRAY[]::text[], NULL, true,
     '{}'::jsonb, NULL, ARRAY['llm:codex:weekly']),
    ('devin', 'cognition', 'free-promo', 'devin_tree', 'DEVIN_API_TOKEN', 1,
     ARRAY['code', 'review', 'validate'],
     ARRAY['swe-2-medium', 'swe-2-high', 'swe-2-max', 'swe-2-flash-medium',
           'swe-2-flash-high', 'swe-2-flash-max'],
     'swe-2-high', false,
     '{"source": "devin models list 2026-11-05 and https://docs.devin.ai/desktop/models", "note": "the six SWE-2 and SWE-2 Flash tiers are Free (docs: SWE-2 free for self-serve Desktop and CLI through 2026-11-30, not cloud sessions); every other family is priced, e.g. gemini-3-8-flash-medium $0.75/$3.75 and gemini-4-argon-medium $2/$10 per 1M in/out; fusion models are paid primaries with a free SWE-2 sidekick"}'::jsonb,
     '2026-11-30', ARRAY['llm:devin:sessions']),
    ('kimi', 'moonshot', 'subscription', 'kimi_tree', 'KIMI_CODE_API_KEY', 1,
     ARRAY['code', 'docs'], ARRAY[]::text[], NULL, false, '{}'::jsonb, NULL,
     ARRAY['llm:kimi:monthly']),
    ('metered', 'openrouter', 'metered', 'api', NULL, 1,
     ARRAY['*'], ARRAY[]::text[], NULL, false, '{}'::jsonb, NULL,
     ARRAY['llm:openrouter:credits'])
ON CONFLICT (name) DO NOTHING;

-- Pool seeds: the rate-less carrier shape (same column list as 0121's seed
-- SQL, plus the reporting columns this migration adds).
INSERT INTO external_rate_limits (
    provider, capacity, refill_per_sec, tokens, daily_cap, "class", unit,
    reset_rule, limit_reported
) VALUES
    ('llm:claude-sub:5h', 1, 1.0, 1, NULL, 'llm-api', 'percent',
     'rolling:18000', NULL),
    ('llm:claude-sub:7d', 1, 1.0, 1, NULL, 'llm-api', 'percent',
     'rolling:604800', NULL),
    ('llm:codex:weekly', 1, 1.0, 1, NULL, 'llm-api', 'percent', NULL, NULL),
    ('llm:kimi:monthly', 1, 1.0, 1, NULL, 'llm-api', 'tokens', NULL, NULL),
    ('llm:devin:sessions', 1, 1.0, 1, NULL, 'llm-api', 'sessions', NULL, 1),
    ('llm:openrouter:credits', 1, 1.0, 1, NULL, 'llm-api', 'usd', NULL, NULL)
ON CONFLICT (provider) DO NOTHING;

COMMIT;

-- End of 0280_llm_account_pools.sql
