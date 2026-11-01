# Runbook — agentlog search traces (retention and kill switch)

Every `search` call appends one best-effort agentlog row with
`meta.trace='search'` (query, mode, kinds, view, session, up to 50 pre-cut
candidates). Agents read them with `get(kind='agentlog', id='/traces')`; the
agent-facing explanation is skill `precis-agentlog-help`. The knobs below are
operator-only, so the skill says "ask a human operator" and points here.

## Knobs

- `PRECIS_SEARCH_TRACE_RETENTION_DAYS` (default 60): retention window for
  trace rows, a positive integer; invalid values fall back to 60. The
  `gc_stale_logs` sweeper physically deletes expired traces (no tombstone),
  independent of ordinary agentlogs' `PRECIS_AGENTLOG_RETENTION_DAYS`
  (default 30) soft-retirement.
- `PRECIS_SEARCH_TRACES` (default on): `0`/`off`/`false`/`no` disables the
  trace write on each search. Re-enable by unsetting it.

Both are set in the worker/server environment and need a restart to take
effect (`docs/runbooks/restart-worker-and-watch.md`). Full variable table:
`docs/reference/config-variables.md`.

## Bounds

A trace keeps at most 50 pre-cut candidates plus `candidates_total`, and at
most 500 query characters plus the original `q_len`. Pool acquisition waits
at most 50 ms; a saturated pool skips the trace, so a missing trace row is
not evidence the search did not run.
