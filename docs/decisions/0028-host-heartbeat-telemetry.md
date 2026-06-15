# ADR 0028 — host heartbeat telemetry for the Status tab

- **Status**: accepted (2026-06-15)
- **Deciders**: Reto + agent
- **Builds on**:
  - ADR 0026 — precis-web surface (the Status tab)
  - migration 0015 — `worker_logs` (host liveness source)
  - `docs/design/system-status-telemetry.md` — this change's plan

## Context

The operator asked for a "system status" view: Claude spend, which
machines are alive, and **core temperatures**. Spend rolls up from
`ref_events.cost_usd`; liveness derives from `worker_logs.host`/`ts`.
Temperature had no source anywhere in the tree.

A new sink was unavoidable for temps. The question was its shape and
how the data gets in.

## Decision

A new `host_heartbeat` table (migration 0017), **latest-snapshot per
host** (PK `host`, reporter UPSERTs), columns `ts, temp_c, load1,
load5, load15, meta`. All sensor columns nullable.

A one-shot `precis heartbeat` CLI is the reporter; each host runs it
on a timer. Temperature is collected best-effort with a
`PRECIS_TEMP_CMD` env escape hatch first, Linux `/sys/class/thermal`
second, `None` otherwise. Load comes from `os.getloadavg()`.

The Status tab reads all three panels via raw SQL on `store.pool`
inside the existing `_safe` wrapper.

## Consequences

- One row per machine; no retention policy needed. A graph-over-time
  view, if ever wanted, is a separate append-only table — not this
  one.
- `temp_c IS NULL` is a first-class state (macOS without a sensor
  command): the host still reports load + liveness.
- No new runtime dependency. `psutil` was rejected (see Alternatives).
- The web layer reads `host_heartbeat` with raw SQL, not the new
  `HeartbeatMixin`, so fake-store route tests need no new method; the
  mixin serves the reporter (write) and db-backed round-trip tests.

## Alternatives considered

1. **Piggyback temp/load onto `worker_logs` payload** (no new table).
   Rejected as the canonical path: `worker_logs` is append-only log
   spool with a hard-cap drop policy; "latest temp per host" would be
   a `DISTINCT ON` scan over a high-churn table, and a log-buffer
   outage would lose telemetry. A dedicated single-row-per-host table
   is the honest model. (The operator selected both options; this ADR
   consolidates them into the table, which subsumes the reporter cadence
   the piggyback option implied.)
2. **Add `psutil`.** Rejected: it does not expose CPU temperature on
   macOS, so the `PRECIS_TEMP_CMD` escape hatch would still be
   required — the dependency buys only `getloadavg`, which is stdlib.
3. **Time-series heartbeat table.** Deferred: the ask is "status right
   now", not history. Snapshot-per-host keeps the table bounded with
   no retention job.
