-- 0091_service_config_concurrency.sql
--
-- Live per-(host,service) concurrency knob on `service_config`, alongside
-- `prio` (migration 0072). A cloud-calling categorizer pass (e.g.
-- `classify`, ADR 0047) claims a batch then makes 2-3 blocking LLM round-
-- trips per row *serially* — almost entirely network-idle time. Bounded
-- in-pass concurrency (a thread pool fanning the per-row cascade out,
-- `workers/classify.py`) parallelizes those calls; this column is the live
-- operator knob for the pool width, resolved the same way `prio` already
-- is (`ServiceConfigResolver`, exact-host-wins-over-`*`, short-TTL cache).
--
-- NULL (the default — no row, or a row with no explicit value) resolves to
-- 1 = today's serial behaviour, so an empty/unset table is byte-identical.
-- The worker additionally clamps the resolved value at a hard env ceiling
-- (`PRECIS_CLASSIFY_MAX_CONCURRENCY`) so a fat-fingered console value can't
-- stampede a cloud endpoint / budget breaker — this column carries no upper
-- CHECK beyond "positive", the ceiling is enforced in code.
--
-- Forward-only (ADR 0005): additive, no data migration.

ALTER TABLE service_config ADD COLUMN IF NOT EXISTS concurrency INT
    CHECK (concurrency IS NULL OR concurrency > 0);

COMMENT ON COLUMN service_config.concurrency IS
    'Live per-host per-service in-pass LLM-call concurrency (thread-pool '
    'width); NULL = default (1, serial). Worker clamps at a hard env '
    'ceiling regardless of this value.';
