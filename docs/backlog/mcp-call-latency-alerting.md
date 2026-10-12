---
status: idea
title: MCP tool-call latency — durable p50/p95 per verb/kind and a health-digest budget check
pillar: platform
---

# MCP tool-call latency: durable trend and alert (gr479849)

Shipped in 83bf6df1b: every dispatch in `server._offload_sync` logs one
`precis.calllat` line (verb, kind, mode, outcome, `duration_ms` including
queue wait, `wait_ms`, result size, no argument text) and feeds an
in-memory last-hour ring (`src/precis/calllat.py`). precis-status shows
`tool_calls_1h` and `tool_latency_1h` p50/p95 for the 5 slowest
verb/kind pairs. The ring is per process and lost on restart, and nothing
alerts on it.

RULED (Reto 2026-10-12, design-fold): read the trend from the existing
durable tool ledger, not a new rollup table.

1. **Durable source.** The `tool_calls` ledger (migration 0133,
   `tool_ledger.record_call`) already stores `latency_ms` per call.
   Compute p50/p95 per verb/kind over the last hour from it for
   precis-status, so the view survives restarts. Check whether the
   ledger's `latency_ms` includes queue wait; if not, and the ring shows
   `wait_ms` carrying the slowness, add a `wait_ms` column in a new
   migration. Not otherwise.
2. **Alert.** A `health_digest._layer1_checks` check goes red when a
   verb/kind's p95 over the last hour exceeds its budget, with a default
   budget and per-verb overrides. It also flags a pair whose p95 more than
   doubled since the last deploy.
3. **Surface-review ranking** reads the same query; no separate store.

Not building: a second latency table, or argument logging.

Who: agent-code.

## Acceptance

- precis-status p50/p95 rows are the same before and after an MCP restart.
- A synthetic slow verb (test fixture) over budget turns the health-digest
  check red; under budget it is green.
