-- 0118_resource_slot_holds.sql
--
-- Crash-safe reclaim for leaked `resource_slots` reservations (2026-08-10
-- fleet-wide outage postmortem: a process killed between
-- `reserve_resource_slots` and `release_resource_slots` leaks the unit
-- forever — the heartbeat UPSERT (`_UPSERT_SLOT`) delta-preserves
-- reservations across a capacity sync, so nothing ever refunds it. Every
-- `llm:*` row on the affected host wedged at `free = 0`.
--
-- `resource_slot_holds` is a lease ledger alongside the bare `free`
-- counter: every reservation the local-serving path takes also records a
-- TTL'd hold row here (`acquire`/`release` in
-- `precis/utils/llm/local_serving.py`). The heartbeat pass sweeps expired
-- holds (`reclaim_expired_slot_holds`) and refunds their units back to
-- `resource_slots.free` — so a killed process's reservation self-heals
-- within one TTL window instead of leaking forever. No FK to
-- `resource_slots`: slot rows are freely deleted/reseeded by the capability
-- probe, and a hold whose slot row is gone simply has nothing to refund.
--
-- Forward-only (ADR 0005): additive, no data migration.

CREATE TABLE IF NOT EXISTS resource_slot_holds (
    id          BIGSERIAL   PRIMARY KEY,
    host        TEXT        NOT NULL,
    resource    TEXT        NOT NULL,
    units       INT         NOT NULL CHECK (units > 0),
    holder      TEXT        NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS resource_slot_holds_expires_at_idx
    ON resource_slot_holds (expires_at);

CREATE INDEX IF NOT EXISTS resource_slot_holds_host_resource_idx
    ON resource_slot_holds (host, resource);

COMMENT ON TABLE resource_slot_holds IS
    'TTL lease per resource_slots reservation. Expired holds are swept by '
    'the heartbeat pass, refunding their units to resource_slots.free — '
    'crash-safe reclaim for a holder killed before release().';

-- End of 0118_resource_slot_holds.sql
