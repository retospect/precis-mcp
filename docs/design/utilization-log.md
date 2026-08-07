# Utilization log — CPU/LLM history for "was the cluster always hot?"

## Problem

The goal-state is a cluster that is never idle: always a simulation,
categorization, or other thinking in flight. Today that question is only
half answerable:

- **LLM utilization** reconstructs fine from `llm_call_log`
  (`ts` + `duration_ms` per call → per-hour duty cycle, gap analysis).
- **CPU utilization has no history.** `host_heartbeat` is
  latest-snapshot-per-host (UPSERT on `host` PK — migration 0017 says so
  explicitly and reserves "a separate append-only table" for the
  time-series). Every 60s beat collects load1/5/15 + temp fleet-wide and
  then overwrites the previous reading.

## Change

1. **Migration `0113_host_heartbeat_log.sql`** — append-only
   `host_heartbeat_log (host, ts, temp_c, load1, load5, load15)`, one
   btree on `(ts)` (serves both the prune and the hourly rollup; at
   4 hosts × 1/min × 14 days ≈ 80k rows a seq scan is also fine). No
   `meta` column — `top_cpu` etc. churn and stay snapshot-only.
2. **Writer** — `workers/heartbeat.py::_collect_and_upsert` additionally
   INSERTs a history row (same values as the UPSERT) and prunes rows
   older than `PRECIS_HEARTBEAT_HISTORY_DAYS` (default 14; `0` disables
   history entirely). Both best-effort: a history failure must never
   fail the liveness UPSERT.
3. **Store** — `HeartbeatMixin.record_heartbeat_history` (INSERT +
   prune in one transaction) and `heartbeat_history(hours=…)` reader.
4. **Report** — `precis stats --utilization [--hours N]` (default 24),
   three sections joining the two logs:
   - `cpu-by-hour` — per host per hour: avg/max load1, max temp.
   - `llm-by-hour` — calls, busy% of wall-clock (sum duration_ms;
     >100% = concurrency), cost, errors.
   - `llm-gaps` — silences > 5 min (the "not always hot" evidence).
   Included in the no-flag default like the other sections.

## Non-goals / follow-ups

- No web sparkline panel yet — the Health tab keeps its snapshot strip;
  a graph over `host_heartbeat_log` is a natural follow-up.
- No GPU-specific utilization (nvidia-smi/powermetrics) — load average
  is the proxy the heartbeat already collects; a GPU gauge would be a
  new probe, out of scope.
- No alerting — `health_digest` can later grow an "idle cluster" check
  reading the same table.

## Rejected

- Widening `host_heartbeat` with an array/JSONB ring buffer — mutating
  hot snapshot rows for history conflates two lifetimes; 0017 already
  named the separate-table design.
- Logging into `worker_logs` — that table is message-shaped and
  host-sparse; numeric columns want their own narrow table.
