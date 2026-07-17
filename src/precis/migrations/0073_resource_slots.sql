-- 0073_resource_slots.sql
--
-- The factory scheduler's resource substrate — one row per (host, resource)
-- the host offers, with a materialized free-slot counter
-- (docs/design/factory-console-and-scheduling.md, slice 6, §5).
--
-- Slice 6b (this migration) stands the table up and has the `heartbeat`
-- self-probe POPULATE it (§5.5): each host discovers what it can do
-- (`gpu`, `podman`, `tts` today — the capability tokens ServiceSpec.requires
-- names) and how many parallel slots it offers, and UPSERTs a row every
-- cycle. Nothing consumes the counter yet — the table is read-only
-- decoration on the `/factory` console until slice 6c wires reserve-at-claim.
-- So an empty (or fully-`free = capacity`) table is byte-identical to
-- today's scheduling.
--
-- `free` is the materialized counter slice 6c decrements inside the claim
-- transaction (`UPDATE ... SET free = free - 1 WHERE free >= 1` — the
-- conditional decrement IS the lock, no race) and restores on job terminal;
-- a crashed holder is reclaimed by the existing lease-expiry sweeper (the
-- job's `meta.lease_until` + PRECIS_STUCK_JOB_HOURS), so no separate lease
-- table is needed — the counter reuses infrastructure already in place.
--
-- `kind` carries the reservation discipline for slice 6c: `hard` = exact,
-- refuse past 0 (gpu, llm concurrency); `soft` = predictive decrement,
-- over-commit allowed, backstop is fail-and-retry (memory — added later).
-- 6b writes only hard capability rows; the soft memory/load signals come
-- with the reserve-at-claim slice.
--
-- Forward-only (ADR 0005): additive, no data migration.

CREATE TABLE IF NOT EXISTS resource_slots (
    host       TEXT        NOT NULL,
    resource   TEXT        NOT NULL,
    capacity   INT         NOT NULL CHECK (capacity >= 0),
    free       INT         NOT NULL,
    kind       TEXT        NOT NULL DEFAULT 'hard' CHECK (kind IN ('hard', 'soft')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (host, resource),
    -- free never exceeds capacity (free = capacity - Σ reservations, and
    -- reservations are non-negative); a soft resource may over-commit past
    -- the line and go below 0, so there is no lower bound here.
    CONSTRAINT resource_slots_free_le_capacity CHECK (free <= capacity)
);

COMMENT ON TABLE resource_slots IS
    'Per-host resource offering + materialized free-slot counter. '
    'kind=hard refuses past 0 (gpu/llm), kind=soft over-commits (memory). '
    'Populated by the heartbeat self-probe; reserved at claim (slice 6c). '
    'Factory scheduler §5.';
