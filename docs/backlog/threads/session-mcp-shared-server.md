# session MCP shared server

**Status:** ends when every Claude Code session on this machine talks to one
supervised shared MCP server that never kills an in-flight call, reports
truthfully what it runs, and gives each session its own DB role and a fair
share. Agent containers and the sandbox sidecar stay stdio and are NOT part
of that end state — backlog/mcp-shared-transport-concurrency.md rules them
out, and that item's role bullet is why: they depend on process-level role
separation this server cannot give them. Today it is one long-lived
streamable-http server, live since 2026-09-29 and dogfooded. The defect
this thread shipped — every bounce killed the calls in flight — is fixed
(gr457887: the drain latches a high-water ticket instead of waiting for an
always-busy server to go idle, and the bound is 120 s and env-tunable), and
re-verified live on the isolated rig. The second defect — a status surface
that could not say which sha a session was talking to, and reported a
three-week-old image build arg instead — is fixed in code (gr457361: the
watched checkout's HEAD outranks the baked env, git-identity fields come
from one lane instead of being mixed, and a live `source_drift` field says
whether the tree has moved past the import) and awaits verification on a
restarted server. What remains is that verification, then the restart-on-drift
half, the parent item's remaining criteria, and the isolation gaps.
**Last reviewed:** 2026-09-30
**Worktree:** `session-mcp-shared-server`

## Do next
1. **Verify gr457361's fix on the live shared server** — the fix is landed
   but unverifiable until the server restarts onto it, and the whole point
   of the fix is that no cheap in-container check substitutes for the
   surface. After the next deploy/bounce, one `precis-status` call must
   show `git_source: watched-checkout`, a `git_sha` matching the mounted
   tree's HEAD at boot rather than the 2026-09-08 image build arg, and
   `source_drift: none`. Then move the tree ahead without bouncing and
   confirm `source_drift: moved <old>→<new>`. Small, but it is the
   acceptance criterion the gripe was filed against.
2. **backlog/mcp-staleness-title-roundtrip-guards.md item 2** — re-opened;
   it was wrongly in No action needed, and it now owns gr458061's
   restart-on-drift half. The closure said the checkout watchdog bounces on
   a HEAD move. It does not for the per-session `precis-mcp-dev-*`
   containers, which are still live serving every pre-flip session and which
   Parked AC5 says must not be swept: `CheckoutWatchdog` only arms when
   `PRECIS_CHECKOUT_WATCHDOG` is set and the dev-stdio launch path never
   sets it, while `InstallWatchdog._fingerprint_for` still returns `None`
   outside `site-packages` as of 54067ee3, which an editable install is.
   Now strictly the remaining half: gr457361's fix makes the drift
   *reportable* on the shared server (`source_drift`), and makes it
   reportable on those dev containers too when they are given
   `PRECIS_CHECKOUT_WATCHDOG`, but reporting is not restarting. Nothing
   stops a process serving stale modules once it has told you it is.
3. **gr457326** — the md-index vector warmup has no retry, so one slow
   embedder batch at boot leaves the cache cold for the process lifetime,
   which is now shared by every session. Degrades silently to lexical.
4. **backlog/session-mcp-http-server.md** — AC2 now has a fix to test
   against (it was written as "precis-status reports the new sha", which
   gr457361 made unpassable) and AC3 (a new verb kwarg surviving a bounce)
   was waiting on gr457887, which has landed and re-verified. Both are
   attemptable; AC2 folds into item 1's single call. Delete the item when
   AC3 and AC5 close.
5. **backlog/mcp-shared-transport-concurrency.md** — the gaps the shared
   process opens: one DB role for every session (measured: no
   PRECIS_MCP_DB_ROLE/_ENFORCE, DSN user agent_rw), no fairness on a
   first-come semaphore, no supervision for a single point of failure whose
   image rebuild bounces every session. Ranked last because its acceptance
   criteria are verified by probing the surface they fix, and that probe is
   only trustworthy once item 1 confirms the surface. The role bullet
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
