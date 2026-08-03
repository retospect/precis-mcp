-- 0104_service_config_expires_at.sql
--
-- Optional TTL for a `service_config` row (§B-2 reserve mode): a nullable
-- `expires_at` so a forgotten override auto-expires rather than depending
-- on an operator to remember to clear it. First (and so far only)
-- consumer is the `reserve` pseudo-service (`workers/service_config.py`:
-- `set_reserve` / `clear_reserve` / `reserve_active`) — a
-- `(host | '*', service='reserve')` row that gates ALL new heavy claims
-- on that host until it expires — but the column is generic for any
-- future TTL'd override, not reserve-specific.
--
-- NULL (every existing row, and every row a non-reserve writer inserts)
-- = no expiry, so an untouched table is byte-identical to today.
--
-- Forward-only (ADR 0005): additive, no data migration.

ALTER TABLE service_config ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

COMMENT ON COLUMN service_config.expires_at IS
    'Optional TTL for this row (the §B-2 reserve pseudo-service uses it to '
    'auto-expire a forgotten reserve); NULL = no expiry.';
