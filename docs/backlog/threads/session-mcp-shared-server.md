# session MCP shared server

**Status:** ends when every session on this machine, including agent
containers, talks to one supervised shared MCP server that never kills an
in-flight call, reports truthfully what it runs, and gives each session its
own DB role and a fair share. Today it is one long-lived streamable-http
server, live since 2026-09-29 and dogfooded; a defect shipped by this
thread kills in-flight calls on every bounce and the status surface cannot
say which sha or process a session is talking to. Fix the kill first, then
make the server truthful, then the parent item's remaining criteria and the
isolation gaps.
**Last reviewed:** 2026-09-30
**Worktree:** `session-mcp-shared-server`

## Do next

1. **gr457887** — a bounce during a slow call kills it: the 20s drain bound
   is under real search latency and the drain waits for in-flight to reach
   zero, which with a dozen sessions may never happen. Live on prod, so every
   ship risks killing someone's query. Not verified until a test keeps
   traffic flowing during a drain (the fixture in
   tests/test_mcp_session_concurrency.py is the candidate).
2. **gr457361** — precis-status reports the image build arg, not the source
   served (measured ~2288 commits stale while the code was same-day current).
   Leverage: "which sha is this session talking to" is unanswerable, which
   blocks the parent item's AC2 and makes 1 and 4 harder to diagnose. Fix
   branch in review: review-and-land, not investigate. gr458039 closed as
   its duplicate; its git_dirty observation is appended there.
3. **gr458038** — the same surface reports a stale uptime (20.6 h against a
   real process age of ~70 min), so it says the server did not restart when
   it did. Same root cause as 2 (_collect_build_info reports baked or
   boot-time identity, not what is live); whoever picks one reads the other,
   a fix that ignores the other leaves the surface lying.
4. **gr457326** — the md-index vector warmup has no retry, so one slow
   embedder batch at boot leaves the cache cold for the process lifetime,
   which is now shared by every session. Degrades silently to lexical.
5. **backlog/session-mcp-http-server.md** — AC3 (a new verb kwarg surviving a
   bounce) is the last criterion that neither passes nor is blocked; it
   cannot be attempted until 1 stops bounces from killing calls. Delete the
   item when it and AC5 close.
6. **backlog/mcp-shared-transport-concurrency.md** — the gaps the shared
   process opens: one DB role for every session (measured: no
   PRECIS_MCP_DB_ROLE/_ENFORCE, DSN user agent_rw), no fairness on a
   first-come semaphore, no supervision for a single point of failure whose
   image rebuild bounces every session. Below 2–3 because its acceptance
   criteria are verified by probing the surface they fix. The role bullet
   decides whether coding jobs can ever leave containers.

## Horizon

1. **backlog/mcp-shared-server-liveness.md** — recovery covers crash and
   clean exit but not a process that is up, listening and wedged, the state
   install_watchdog exists to escape; `--restart unless-stopped` never fires
   on it. One wedge is twelve dead sessions and nobody owns the server.
2. **backlog/mcp-shared-transport-concurrency.md**, role isolation — one
   process holds one DB role for every session, and agent_container.py
   depends on that separation to make a read-only agent's writes fail in
   Postgres. SET ROLE is ruled out under transaction pooling, so per-role
   pools or session-keyed authz. An authz boundary, not throughput.
3. **backlog/mcp-shared-transport-concurrency.md**, fairness — sizing the
   semaphore is not fairness; one session's burst holds every permit while
   another's cheap read queues, and it gets reported as "the MCP is slow".
4. **backlog/mcps-venv-deploy-gaps.md** — the shared server is a hand-rolled
   dev-machine service by decision (Reto, 2026-09-29); whether the fleet
   mcps role adopts this shape decides if the ensure script stays a wrapper
   or becomes an Ansible role. Until then the two must not entangle.
5. **backlog/mcp-shared-server-multiprocess.md** — `--workers 1` is correct
   and is the ceiling; the inventory of what must leave process memory
   first. Arc, not work: 12 concurrent searches finish in 4.78 s against a
   2.06 s single call.
6. **K-parallel exercise-mcp on dev** (unfiled) — the acceptance gate that
   proves 2 and 3 worked; earlier it measures an idle server.
7. **pgbouncer cl_waiting observability** (Reto) — prerequisite for every
   future claim about pool headroom; without it 6 cannot tell "pool
   starved" from "something else slow".

## Parked

- **backlog/session-mcp-http-server.md AC5** (one container after a day) —
  unparks when the last pre-flip session ends; the surviving
  precis-mcp-dev-* containers must not be swept, pre-flip sessions still
  talk to them over stdio.
- **pgbouncer admin-console read access** (Reto) — unparks when granted:
  cl_waiting is the only direct evidence of pool starvation; until then
  pool-headroom statements are inference from backend counts.
- **K-parallel exercise-mcp load harness** — specced inside
  backlog/mcp-shared-transport-concurrency.md; unparks when the role and
  fairness gaps in 6 are closed, before that it measures an idle server.
- **live cross-session serve-ledger check** — unfiled; unparks when a second
  session can fetch a slug this one just fetched and report full-serve vs
  stub.

## No action needed

- **backlog/mcp-staleness-title-roundtrip-guards.md item 2** — closed: the
  stdio launcher is deleted and the checkout watchdog bounces on a HEAD move.
  Item 1 of that file is unrelated and stays open.
- **stale-serve leak / scripts/reap-stale-serves** — measured absent across
  the cluster and moot on the shared server.
- **pgbouncer and embedder saturation** — measured 2026-09-30T06:44Z: 17/100
  connections, embedder queue-wait 0, 0 shed. Supersedes any reading of
  gr450123 as an ongoing incident.
- **stdio-vs-shared-HTTP as an open decision** — settled by the deployment;
  server.py docstring corrected in 550f9ae6.
