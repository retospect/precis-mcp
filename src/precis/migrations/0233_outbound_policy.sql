-- 0233_outbound_policy.sql
--
-- docs/backlog/outbound-policy.md slice S1 / decision D4: one outbound
-- policy layer over `external_rate_limits`. Adds the per-row override
-- columns the new resolver (`utils/outbound_policy.py`) reads — any
-- non-NULL column overrides the code layers (class default ->
-- POLICIES[service]) for that provider row:
--
--   class          — policy class override ('scraped-host',
--                    'provider-api', 'llm-api', 'llm-local', 'internal');
--                    NULL = infer in code (Policy.cls, host shape).
--   max_wait_s     — bound on a limiter/slot wait for this row.
--   connect_s /
--   read_s /
--   total_s        — per-phase/overall request timeout overrides.
--   on_refuse      — 'wait' | 'defer' | 'fail'.
--   concurrency    — resource_slots resource to hold during the call.
--
-- State columns shared by every class:
--
--   parked_until   — Retry-After park end, so a rate-less row (llm-api,
--                    no token lane) can still be parked cross-host.
--   last_used_at   — set by acquire and by the rate-less pre-call check;
--                    a rate-less row never moves `last_refill`, so without
--                    this the host-row eviction would retire a busy row.
--   refused_today  — refusal counter, reset with day_used on the
--                    day_start rollover inside the consume statement.
--   last_refused_at — last refusal timestamp.
--
-- Also seeds the llm-api concurrency slot row: one `('api',
-- 'llm-api:openrouter')` resource_slots row caps fleet-wide OpenRouter
-- calls in flight; 'api' is an accounting label, not a host. Operator
-- edits capacity on this row.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS "class"        text
    CHECK ("class" IN ('scraped-host', 'provider-api', 'llm-api',
                       'llm-local', 'internal'));
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS max_wait_s     numeric;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS connect_s      numeric;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS read_s         numeric;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS total_s        numeric;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS on_refuse      text
    CHECK (on_refuse IN ('wait', 'defer', 'fail'));
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS concurrency    text;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS parked_until   timestamptz;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS last_used_at   timestamptz;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS refused_today  integer NOT NULL DEFAULT 0;
ALTER TABLE external_rate_limits ADD COLUMN IF NOT EXISTS last_refused_at timestamptz;

INSERT INTO resource_slots (host, resource, capacity, free, kind)
VALUES ('api', 'llm-api:openrouter', 16, 16, 'hard')
ON CONFLICT (host, resource) DO NOTHING;

COMMIT;

-- End of 0233_outbound_policy.sql
