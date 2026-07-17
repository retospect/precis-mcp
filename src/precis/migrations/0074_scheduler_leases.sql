-- 0074_scheduler_leases.sql
--
-- The decentralized recurring-work trigger's lease clock — one row per
-- folded thin-timer cadence (cron_tick, watch_poll, …), with a `next_fire_at`
-- that an atomic conditional advance claims
-- (docs/design/factory-console-and-scheduling.md, slice 10, §15i).
--
-- §15i's decision: "there ought to be only one scheduler", and its
-- exactly-once guarantee belongs in Postgres, not in a designated node (a
-- SPOF — down when a fire is due ⇒ missed fire). So minting is
-- DECENTRALIZED: every worker runs the `scheduler` pass each cycle, and
-- claiming a due cadence is the reserve-at-claim pattern (§5.2) —
--
--   UPDATE scheduler_leases
--      SET next_fire_at = now() + interval, last_fired_at = now(), last_host = :h
--    WHERE name = :n AND next_fire_at <= now() RETURNING name;
--
-- Only one worker's UPDATE matches the row (the rest see it already
-- advanced) — the advance IS the lock. A worker being down never drops a
-- fire: any other live worker mints it. The advance is `now() + interval`
-- (not `next_fire_at + interval`), so a fleet-wide outage collapses to a
-- single catch-up fire on recovery, not a backlog burst.
--
-- Ships DARK (slice 10 Phase-1): the `scheduler` pass is off by default
-- (no default profile, `PRECIS_SCHEDULER_ENABLED` unset), so the standalone
-- launchd timers still own the ticks — no double-fire. An empty table is
-- byte-identical to today. The Phase-2 window flips the flag on and retires
-- the timers.
--
-- The cadence's *interval* is a code fact (the `workers.scheduler` registry),
-- passed into the advance — so the table carries only the lease clock, and a
-- code-side cadence change takes effect on the next claim. `interval_s` is
-- stored purely for observability (the /factory console reads it).
--
-- Forward-only (ADR 0005): additive, no data migration.

CREATE TABLE IF NOT EXISTS scheduler_leases (
    name          TEXT        PRIMARY KEY,
    interval_s    INT         NOT NULL CHECK (interval_s > 0),
    next_fire_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_fired_at TIMESTAMPTZ,
    last_host     TEXT
);

COMMENT ON TABLE scheduler_leases IS
    'Decentralized recurring-work lease clock — one row per folded thin-timer '
    'cadence. The conditional advance (next_fire_at <= now()) IS the lock: '
    'exactly-once minting across the fleet with no designated node. '
    'Factory scheduler §15i, slice 10.';
