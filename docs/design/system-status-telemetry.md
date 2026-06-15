# Design — system status telemetry (Claude spend, host liveness, temps)

Status: implemented (2026-06-15)

## Goal

The operator wants a single "system status" view answering three
questions at a glance:

1. **Claude usage** — how much am I spending, on what models, where?
2. **Which machines are alive** — and are any throwing errors?
3. **Core temps** — is any box running hot?

## Data sources

| Need            | Source                              | New plumbing? |
|-----------------|-------------------------------------|---------------|
| Claude usage    | `ref_events.cost_usd` + `payload`   | no            |
| Host liveness   | `worker_logs(host, ts, level)`      | no            |
| Core temps/load | `host_heartbeat` (new)              | yes           |

`ref_events` already carries `cost_usd`, `duration_ms`, and a JSONB
`payload` with `model` / `turns_used` (written by
`precis.utils.claude_agent.call_claude_agent`'s `log_event` hook on
`agent:done`). `worker_logs` (migration 0015) carries one row per log
line with `host` + `ts` + `level`, so `max(ts)` per host is liveness
and a `FILTER (WHERE level IN ('WARNING','ERROR'))` count is recent
trouble.

Temperatures have **no** existing source, hence the new table.

## host_heartbeat

`migrations/0017_host_heartbeat.sql`: one row per host (PK `host`),
UPSERTed by a reporter. Columns: `ts`, `temp_c`, `load1/5/15`, `meta`.
Latest-snapshot, not a time series — answers "is it hot now?" without
a retention policy. All sensor columns nullable (`temp_c` is
best-effort).

Store surface (`precis.store._heartbeat_ops.HeartbeatMixin`):

- `record_heartbeat(host, *, temp_c, load1, load5, load15, meta)` — UPSERT.
- `recent_heartbeats()` → `list[HostHeartbeat]` ordered by host.

## Reporter — `precis heartbeat`

A one-shot CLI (`precis.cli.heartbeat`) each machine runs on a timer:

- `host` ← `PRECIS_HOST_NAME` env or `socket.gethostname()` (same
  resolution as the DB log handler, so heartbeat and log rows agree
  on host identity).
- `load1/5/15` ← `os.getloadavg()` (always available on the unix
  hosts in play; `None` on platforms without it).
- `temp_c` ← best-effort, in priority order:
  1. `PRECIS_TEMP_CMD` env — run it, parse the first float from
     stdout as °C. The escape hatch for any sensor (macOS
     `osx-cpu-temp`, IPMI, a custom script) without baking
     platform logic in.
  2. Linux — max over `/sys/class/thermal/thermal_zone*/temp`
     (millidegrees ÷ 1000).
  3. otherwise `None`.

No new dependency: load comes from stdlib, temp from a file read or a
subprocess the operator wires up. Adding `psutil` was rejected — it
doesn't read CPU temp on macOS anyway, so it wouldn't remove the
`PRECIS_TEMP_CMD` escape hatch.

## Status tab

`precis_web.routes.status` gains three `_safe`-wrapped query helpers
(`_claude_usage`, `_hosts`, `_heartbeats`) reading via raw SQL on
`store.pool` — same defensive pattern as the existing panels, so a
schema surprise or the test fake's empty cursor degrades to an empty
panel rather than a 500. Staleness ("last seen 3m ago", a red dot
past a threshold) is computed in Python from the timestamps.

The web layer reads `host_heartbeat` with raw SQL rather than the
store mixin so the fake-store route tests don't need to implement the
method; the mixin exists for the reporter (write) and db-backed tests
(round-trip).
