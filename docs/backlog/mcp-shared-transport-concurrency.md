---
status: draft
title: The shared HTTP session server is live — close the three gaps it opens
pillar: platform
---

# Shared MCP transport: what still has to be proven now that agents DO share a process

## Motivation / why
Pipelines are being set up to run many agents concurrently, and the
session MCP already answers that with one long-lived
`precis serve --transport streamable-http` container shared by every
Claude Code session. This item was first filed as "should we?"; that is
settled — it shipped, sized for the fan-out
(`PRECIS_MCP_TOOL_CONCURRENCY` and `PRECIS_DB_POOL_{MIN,MAX}_SIZE`).
What remains is the part sizing does not cover.

Correcting the record, because the earlier draft of this item got it
wrong and the stale `server.py` module docstring is why: that docstring
still claimed stdio was "every existing caller's transport" and that
"nothing else uses" the network transport, months after the shared
server landed. It has been rewritten to describe both deployments. A
reader orienting from the owning docstring — the repo's prescribed
reading order — was being told the opposite of the deployment.

What the shared process genuinely removed: `min_size` held per process,
pgbouncer's `max_client_conn = 200` ceiling, the stale-serve leak
`scripts/reap-stale-serves` sweeps, per-session boot cost. What it does
**not** touch: pgbouncer's `default_pool_size = 25` and the embedder's
`max_inflight = 4`, the actual throughput ceilings, which sit downstream
of the transport and are shared either way.

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
- **Role isolation under one process.** Live, not hypothetical: the
  shared server holds one role for every attached session, because
  `_apply_db_role` reads process-level `PRECIS_MCP_DB_ROLE` in the
  pool's per-connection configure hook. `agent_container.py` depends on exactly that separation to make
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
Unwinding the shared transport, or moving spawned callers (agent
containers, the sandbox sidecar, `asa_bot`) onto it — they stay stdio,
and the role bullet below is why. Also not in scope: changing
`_DEFAULT_TOOL_CONCURRENCY` or the pool *defaults*, which govern the
stdio callers; the shared server sets its own via env.

## Acceptance criteria
- A decision, recorded here, on whether coding jobs can ever leave
  containers — it turns entirely on the role-isolation bullet.
- The pool-storm and role-isolation tests exist and are honest about
  what they show (the role one is expected to document a gap, not a
  passing property).
- A fairness policy between sessions, and supervision: one shared
  process means one crash, or one image rebuild, takes every session
  down at once. Sizing and the pool env knob
  (`pool.resolved_pool_max_size`) already shipped.

## Target + blast radius
`src/precis/server.py`, `src/precis/store/pool.py`,
`src/precis/workers/executors/agent_container.py`,
`deploy/roles/pgbouncer/`.

## Measured 2026-09-30T06:44Z — downstream is idle, so headroom is the question

A read-only pass while the fleet ran gated code and several sessions were
open. Scope matters: this covered the four cluster nodes, NOT the dev
machine that hosts the shared session server, so it says nothing about
the shared process itself.

- **Cluster `precis serve`: all stdio session children.** One node had
  two, each parented by its own session pid; the other three had none.
  No ppid-1 daemons, no `--transport` flags, and no same-ppid pair, so
  the stale-serve leak was not present. The shared HTTP server is a
  dev-machine deployment only — cluster-side execution is still
  process-per-caller.
- **Postgres: 17 of 100 connections.** `agent_rw` 1 active / 11 idle;
  0 idle-in-transaction; `rolconnlimit` unlimited on both agent roles.
  The 11 idle on one role is consistent with the shared server's pool
  sitting between its min and max — i.e. the pool is doing its job and
  holding, not leaking.
- **pgbouncer `SHOW POOLS`: NOT READ.** The admin console requires
  auth. `cl_waiting` is the one number that would actually prove or
  disprove pool starvation, and it remains unmeasured — inferring it
  from backend counts is not the same thing. Getting read-only admin
  access is a prerequisite for any honest claim here.
- **Embedders: zero backpressure.** The two nodes that run the daemon
  both ready, `queue_wait_seconds` total and max both 0.000, 0 shed.
  Idle at `max_inflight = 4`.

So none of the shared ceilings are being approached at current load.
That makes this a headroom question for the planned fan-out rather than
a live fire, and it means a "looks fine" result from poking the shared
server today would carry almost no information.

Observed the same day, unprompted: a sibling session rebuilt the shared
container's image because its venv was missing deps, bouncing it once
for every attached session. That is the supervision bullet above
happening in practice — the shared server is a single point of failure
for every session's tool surface, and routine maintenance on it is a
fleet-wide interruption.

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
