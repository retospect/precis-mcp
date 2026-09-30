# session MCP shared server

**Status:** ends when every Claude Code session on this machine talks to one
supervised shared MCP server that never kills an in-flight call, reports
truthfully what it runs, and gives each session its own DB role and a fair
share. Agent containers and the sandbox sidecar stay stdio and are NOT part
of that end state — backlog/mcp-shared-transport-concurrency.md rules them
out, and item 4's role bullet is why: they depend on process-level role
separation this server cannot give them. Today it is one long-lived
streamable-http server, live since 2026-09-29 and dogfooded. The defect
this thread shipped — every bounce killed the calls in flight — is fixed
(gr457887: the drain latches a high-water ticket instead of waiting for an
always-busy server to go idle, and the bound is 120 s and env-tunable), and
re-verified live on the isolated rig. What remains is a status
surface that cannot say which sha or process a session is talking to, then
the parent item's remaining criteria and the isolation gaps.
**Last reviewed:** 2026-09-30
**Worktree:** `session-mcp-shared-server`

## Do next

1. **gr457361** — precis-status reports the image build arg, not the source
   served (measured: reported f2cbcb29, built 2026-09-08, while serving code
   542 commits newer the same day). Worse than unanswerable: gr458061 caught
   the surface printing started_at 2026-09-29T15:34:31 beside uptime_seconds
   54369 (~15.1 h) when only one could be true, and the process behind it
   served 16-hour-stale in-memory modules while its bind-mounted files were
   current — which produced two detailed false root-cause analyses
   (gr457995, gr457996, both refuted) and let a join through that the
   current code refuses. So the acceptance criterion is not "the right sha
   appears" but that sha, started_at and uptime are mutually consistent, and
   that the served source is distinguished from the image build. No cheap
   check substitutes: in that container stat, grep and a fresh `python -c`
   import all reported the new code. That is why this blocks dogfooding
   generally, not just this thread. Not started: no branch on origin and no
   live tree claims it as of 2026-09-30. gr458039 closed as its duplicate
   (git_dirty observation appended there); close gr458061 as one too when
   this lands — it is the evidence record, hexa is not touching src/precis/.
2. **gr457326** — the md-index vector warmup has no retry, so one slow
   embedder batch at boot leaves the cache cold for the process lifetime,
   which is now shared by every session. Degrades silently to lexical.
3. **backlog/session-mcp-http-server.md** — AC3 (a new verb kwarg surviving a
   bounce) is the last criterion that neither passes nor is blocked — it
   was waiting on gr457887, which has landed and re-verified, so it is
   attemptable now. Delete the item when AC3 and AC5 close.
4. **backlog/mcp-shared-transport-concurrency.md** — the gaps the shared
   process opens: one DB role for every session (measured: no
   PRECIS_MCP_DB_ROLE/_ENFORCE, DSN user agent_rw), no fairness on a
   first-come semaphore, no supervision for a single point of failure whose
   image rebuild bounces every session. Below 1 because its acceptance
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
  fairness gaps in Do-next 5 are closed, before that it measures an idle server.
- **live cross-session serve-ledger check** — unfiled; unparks when a second
  session can fetch a slug this one just fetched and report full-serve vs
  stub.

## No action needed

- **backlog/mcp-staleness-title-roundtrip-guards.md item 2** — closed: the
  stdio launcher is deleted and the checkout watchdog bounces on a HEAD move.
  Item 1 of that file is unrelated and stays open.
- **gr458038** — refuted: my own filing, retracted as a measurement error.
  The precis-status uptime I called stale came from a pre-flip stdio
  container (`precis-mcp-dev-59558`) while the "real" age I compared it to
  was `precis-mcp-http`; `_STARTED_AT` is captured at module import and
  cannot survive a restart. It was ranked here as sharing gr457361's root
  cause — it does not, and landing that fix has no bearing on it.
- **stale-serve leak / scripts/reap-stale-serves** — measured absent across
  the cluster and moot on the shared server.
- **pgbouncer and embedder saturation** — measured 2026-09-30T06:44Z: 17/100
  connections, embedder queue-wait 0, 0 shed. Supersedes any reading of
  gr450123 as an ongoing incident.
- **stdio-vs-shared-HTTP as an open decision** — settled by the deployment;
  server.py docstring corrected in 550f9ae6.
