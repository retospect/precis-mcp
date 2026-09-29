---
status: draft
title: Decide stdio-per-agent vs one shared HTTP process, on measurements rather than intuition
---

# Shared MCP transport: what has to be re-proven before agents share a process

## Motivation / why
Pipelines are being set up to run many agents concurrently. Today
`precis serve` is stdio and Claude Code spawns one process per session,
so every concurrency bound in the server is a *per-agent* bound. A
shared network transport (`server._NETWORK_TRANSPORTS`) would remove
several real per-process costs, but it converts three per-agent
properties into fleet-wide ones — and nothing measured any of them,
because until `tests/_mcp_session.py` no test had ever created an MCP
session at all.

What a shared process genuinely removes: `min_size=2` held per process
(`store/pool.py`), pgbouncer's `max_client_conn = 200` ceiling, the
stale-serve leak that `scripts/reap-stale-serves` exists to sweep, and
per-agent boot cost. What it does **not** touch: pgbouncer's
`default_pool_size = 25` and the embedder's `max_inflight = 4`, which
are the actual throughput ceilings and sit downstream of the transport.

## In scope
Three tests now pin the per-session layer
(`tests/test_mcp_session_concurrency.py`): cross-session head-of-line
blocking stays fixed, the tool-concurrency cap is process-wide, and the
serve ledger does not leak between sessions.

The open work is the tier that needs a real database:

- **Pool behaviour under a session storm.** N concurrent sessions × M
  calls against the per-pytest-session `precis_test_<uuid>` clone
  (`tests/conftest.py`): no `PoolTimeout`, bounded wait, no deadlock,
  clean teardown. Watch for the suite's lock-holding-connection leak
  hard-fail, which a connection storm is the most likely test to trip.
- **Role isolation under one process.** `_apply_db_role` reads
  process-level `PRECIS_MCP_DB_ROLE` in the pool's per-connection
  configure hook, so two sessions in one process cannot hold different
  roles. `agent_container.py` depends on exactly that separation to make
  a read-only agent's writes fail in Postgres rather than merely lack a
  tool. `store/pool.py` also rules out the obvious fix: under pgbouncer
  transaction pooling a session `SET ROLE` neither persists nor stays
  contained. Separate pools per role, or app-level authz keyed on the
  session, are the two candidate shapes.
- **Fairness.** `server._get_tool_semaphore` is a first-come-first-served
  singleton, so under a shared process one session bursting calls can
  hold every permit while another session's cheap read queues. There is
  no fairness property to assert today; a shared transport needs one.

## Explicitly NOT in scope
Building the shared transport. This item decides whether to, and names
what would have to be true first. Also not in scope: changing
`_DEFAULT_TOOL_CONCURRENCY` or the pool defaults — those are outputs of
the measurement, not inputs.

## Acceptance criteria
- A decision, recorded here, on whether coding jobs can ever leave
  containers — it turns entirely on the role-isolation bullet.
- The pool-storm and role-isolation tests exist and are honest about
  what they show (the role one is expected to document a gap, not a
  passing property).
- If the answer is "build it": `PRECIS_MCP_TOOL_CONCURRENCY` sizing, a
  pool-size env knob (`Store.connect` takes `min_size`/`max_size` but
  nothing reads an env var), a fairness policy, and supervision — one
  shared process means one crash takes every agent down.

## Target + blast radius
`src/precis/server.py`, `src/precis/store/pool.py`,
`src/precis/workers/executors/agent_container.py`,
`deploy/roles/pgbouncer/`.

## Open questions / decisions log
- Confirmation tier, after the above: `scripts/exercise-mcp/run.sh`
  already renders an MCP config and drives `claude -p` against a
  containerized serve. K of those in parallel, recording pgbouncer
  `SHOW POOLS` (`cl_waiting`, `sv_active`), the embedder's
  `precis_embedder_queue_wait_seconds_max` and shed counter, serve RSS,
  and latency percentiles. Against dev — the session MCP targets prod
  with `agent_rw`. This answers whether an agent *experiences*
  degradation; it is expensive and noisy, so it is an acceptance gate,
  not an investigation tool.
- Prior art that this is a real failure mode, not a hypothetical:
  gr450123 (N sibling MCP containers × `max_inflight` against one
  embedder) was found in production and fixed with the bounded queue
  wait, not by testing.
