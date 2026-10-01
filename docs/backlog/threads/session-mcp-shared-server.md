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
always-busy server to go idle, and the bound is 120 s and env-tunable),
re-verified on the isolated rig and now **confirmed in production**: the
2026-09-30T23:14:30Z bounce logged `drained 1 in-flight call(s)` on a real
session's call, which under the old design is the call that would have been
killed. The three bounces around it drained 0, so the fix is exercised by
ordinary traffic rather than only by a rig. The second defect — a status surface
that could not say which sha a session was talking to — is fixed and
verified (gr457361: the watched checkout's HEAD outranks the baked env,
git-identity fields come from one lane instead of being mixed, and a live
`source_drift` field says whether the tree has moved past the import;
the 11:10Z boot banner reports `[watched-checkout]` against the mounted
tree's HEAD, and `watched-checkout` is a string the fix introduced, so the
process naming the lane is the process running it). Note for anyone
re-measuring: the defect had two faces, and this thread quoted only one of
them. On the dev-stdio containers it printed a confidently wrong sha
(`f2cbcb29`, an image build arg from 2026-09-08); on `precis-mcp-http`,
whose image was later rebuilt from a worktree with no `.git` in the build
context, it printed `unknown (unknown) [unknown] unknown` — no sha at all.
The migration is done: Reto decided on 2026-09-30 to retire the
per-session stdio containers rather than harden them (td458385), and on
2026-10-01 every session reconnected onto this server when sessions
resumed — `docker ps` now shows `precis-mcp-http` alone (AC5 passes). That
makes it the single point of failure for every session, so wedge detection
is next, then the isolation gaps.
**Last reviewed:** 2026-09-30 (pillar review same day added gr345270 and a
server-side-session-context Horizon pointer)
**Worktree:** `session-mcp-shared-server`

## Do next
1. **gr459481 — a long enough dark window permanently disconnects idle
   interactive sessions. Decided by Reto 2026-10-01: hold the port across
   restarts, watchdog stays on, build it all, then land together. BUILT,
   not yet landed or verified live.**
   11:14-11:16Z: two qlands 17 s apart bounced `precis-mcp-http` twice and
   at least three interactive sessions lost `precis` with ECONNRESET until
   a human reconnected them. The server was fine both times.

   **Mechanism, measured on the 8766 rig 2026-10-01 12:03-13:23Z** with one
   throwaway interactive window (same Claude Code build as every live
   session): an idle session holds a `GET /mcp` event stream; when it dies
   the client *does* retry, but only within a budget. A ~13 s dark window
   (one watchdog restart) and a pair with a 2 s up-gap both recovered on
   their own within ~3 s of the port returning. A continuous 41 s window
   and a 24 s window both left it ✘ until a manual reconnect. So the budget
   is between 16 s and 24 s; the incident pair was ~25-30 s dark with no
   usable gap. A single restart today is under the budget, with a few
   seconds of margin — which a slow boot under load can eat, so a watchdog
   debounce alone is not a fix. There is no slow second retry: a clean
   rerun of the 24 s window (13:02:31-13:02:58Z, window untouched) saw no
   client request in the following 20 min.

   **Built (unlanded, this worktree):** `src/precis/mcp_supervisor.py`
   becomes PID 1, binds the port once, runs `--prepare` (the `/src` → `/app`
   re-copy) before every child, and runs `precis serve --fd {fd}` on the
   inherited socket. A child exiting 0 (the watchdog) is replaced at once;
   a crash is replaced after an exponential backoff; SIGTERM is forwarded,
   so `docker stop` still works. Between children, connections wait in the
   listen backlog instead of being refused — the dark window is gone, not
   shortened. Stdlib only, no `precis.*` imports and no lazy imports: it
   outlives every `/app` re-copy, so the container copies it to `/tmp` and
   runs it from there, and changing it takes a container recreate.
   The wedge detector (`mcp_liveness.py`) runs inside it under the same
   import rule; SIGSTOP and 12-session-burst demonstrations passed on a
   rig 2026-10-01 14:28-14:41Z.

   **Supervised rig, 2026-10-01 13:23-13:47Z:** 61 initialize calls across
   a restart, 0 refused, slowest 6.2 s (queued through the respawn). With
   the throwaway window attached, two restarts 17 s apart: its event-stream
   reconnect reached a live server and got **404** (stale session) at
   13:46:42Z and 13:46:43Z, then the client stopped re-opening the stream —
   it does not re-initialize on a GET 404 by itself — but its next tool
   call did, silently: 14:08:55Z POST 404 → initialize 200 → the call
   200, within 10 ms, no human reconnect. **The fix holds in the
   interactive client.**

   **Left, in order:** (b) land everything together; (c) install the staged launch — the
   ensure script's `RUN_CMD` switched to the supervisor, with a fallback to
   the old launch when the checkout lacks it. ⚠ Every SessionStart runs
   `precis-mcp-http-ensure.sh` (repo `.claude/settings.json`), and its
   env hash covers the script's own bytes, so **editing the script
   recreates the shared server at the next session start anywhere** —
   install it only as the coordinated cutover, after the land, told to
   `deploy` first. That recreate is the last restart that strands
   sessions.

   **Next after that (Reto 2026-10-01): a `production` branch.** Every
   qland moves the main checkout and restarts the server; the server should
   instead track a `production` branch that `scripts/deploy` fast-forwards
   after each deploy (`/go` and `/qgo` alike — it means "what the cluster
   runs", which is what a server writing to the prod DB should run). The
   container mounts a dedicated worktree on that branch instead of the main
   checkout, so restarts drop from every qland to every deploy. Branch
   protected, fast-forward only. Dogfooding an unlanded verb then means
   `/qgo` or the 8766 rig. Tell `deploy` before touching `scripts/deploy`.

   Out of scope: running the server stateless (removes the 404 round-trip,
   costs the serve ledger's per-session dedup and server push).

2. **backlog/embedder-capacity-ownership.md — reduced to the admission
   question.** gr459088 and gr457326 are CLOSED, verified on the shared
   server 2026-10-01 03:00Z: **1% → 84% of blocks indexed**, cache
   1.05 MB → 45 MB, after thirteen hours of zero progress. Five fixes,
   none sufficient alone — per-batch retry (d9bd4e16), cold-cache re-arm
   (2b729531), 16-block/25 000-char cap (5146528d), skip-and-continue
   (ae7bdb6a), live warmup state (10f307e5).

   Root cause for anyone who finds this later: the warm pass starved the
   service it was waiting on. 64-block batches collected the long blocks
   (up to 22756 chars), blew the 15 s client budget, and each timeout
   orphaned a server-side computation that kept its slot — so the pass
   manufactured the saturation that rejected its own retries, and
   `embed_missing` sending the first 64 *missing* blocks made batch 1
   always the worst one. Two intermediate diagnoses were wrong and both
   fell to measurement rather than argument: "the embedder sheds
   continuously" (idle between passes — inflight 0 on six samples) and
   "slots are leaked" (they release when the pass stops). Timed idle the
   embedder does one short string in 0.17 s — **the hardware was never
   the constraint.**

   What is left is server-side admission: a warm batch and a one-string
   query share four undifferentiated slots, so a pass still crowds out
   interactive embeds for its window. Narrow now that passes complete.
   The completed warm (~19400 vectors, 79 MB) exposed `add()`'s per-vector
   `np.vstack` as O(n^2); now a capacity-doubling buffer behind `_rows()`.

3. **backlog/session-mcp-http-server.md** — AC2 passes now: it was written
   as "precis-status reports the new sha", which gr457361 made unpassable,
   and the 11:10Z banner
   (`precis-mcp 8.35.1 @ 05ce7657ceef (main) [watched-checkout] /src`)
   satisfies it. AC5 passes as of 2026-10-01T11:00Z (one container). Left:
   AC1 in the session client and AC3, which closes opportunistically on the
   next verb-signature change someone else lands. Delete the item when both
   close.
4. **gr458350** — prod credentials passed to `docker run` as `-e` values,
   so `docker inspect` prints them in cleartext. The per-session launcher
   is gone with its containers; checked by name only on 2026-10-01, the
   shared server's ensure script does the same — `PRECIS_DATABASE_URL`,
   four API keys and `PRECIS_MCP_TOKEN` all sit in `precis-mcp-http`'s
   `.Config.Env`. One launcher to fix now, outside the repo
   (`~/work/infrastructure/precis-mcp/scripts/precis-mcp-http-ensure.sh`).
   The fix needs a read-only mount of the password directory added first —
   `precis-mcp-http` mounts only `/data/corpus`, `/data/notes` and `/src`,
   unlike the retired stdio launcher, which did mount it. Reto
   deferred rotation on 2026-09-30 ("later"); the leak path is the half
   fixable without a rotation window. Inspect with the names-only format in
   the gripe.
5. **backlog/mcp-shared-transport-concurrency.md** — the gaps the shared
   process opens: one DB role for every session (measured: no
   PRECIS_MCP_DB_ROLE/_ENFORCE, DSN user agent_rw), no fairness on a
   first-come semaphore, no supervision for a single point of failure whose
   image rebuild bounces every session. Ranked last because its acceptance
   criteria are verified by probing the surface they fix — that surface is
   now trustworthy, so this is unblocked rather than waiting. The role
   bullet decides whether coding jobs can ever leave containers.

## Horizon

1. **backlog/mcp-shared-transport-concurrency.md**, role isolation — one
   process holds one DB role for every session, and agent_container.py
   depends on that separation to make a read-only agent's writes fail in
   Postgres. SET ROLE is ruled out under transaction pooling, so per-role
   pools or session-keyed authz. An authz boundary, not throughput.
2. **backlog/mcp-shared-transport-concurrency.md**, fairness — sizing the
   semaphore is not fairness; one session's burst holds every permit while
   another's cheap read queues, and it gets reported as "the MCP is slow".
3. **backlog/mcps-venv-deploy-gaps.md** — the shared server is a hand-rolled
   dev-machine service by decision (Reto, 2026-09-29); whether the fleet
   mcps role adopts this shape decides if the ensure script stays a wrapper
   or becomes an Ansible role. Until then the two must not entangle.
4. **backlog/mcp-shared-server-multiprocess.md** — `--workers 1` is correct
   and is the ceiling; the inventory of what must leave process memory
   first. Arc, not work: 12 concurrent searches finish in 4.78 s against a
   2.06 s single call.
5. **K-parallel exercise-mcp on dev** (unfiled) — the acceptance gate that
   proves 1 and 2 worked; earlier it measures an idle server.
6. **pgbouncer cl_waiting observability** (td458386) — granted by Reto
   2026-09-30, not yet wired; prerequisite for every future claim about
   pool headroom, because without it entry 5 cannot tell "pool starved"
   from "something else slow".
7. **backlog/server-side-session-context.md** — owned by
   graph-memory-consumers, not this thread; pointer only. Precondition
   td458385 (sessions moving to this server) is this thread's own
   Do-next 1.

## Parked

- **pgbouncer admin-console read access** (td458386) — **granted by Reto
  2026-09-30**; unparks when the coordinates are in the overlay and a
  `SHOW POOLS` read works from a script. cl_waiting is the only direct
  evidence of pool starvation, so until it lands the 17/100-connections
  reading and every other pool-headroom statement stay inference from
  backend counts and should be re-derived, not inherited.
- **K-parallel exercise-mcp load harness** — specced inside
  backlog/mcp-shared-transport-concurrency.md; unparks when the role and
  fairness gaps in Do-next 6 are closed, before that it measures an idle server.
- **live cross-session serve-ledger check** — unfiled; unparks when a second
  session can fetch a slug this one just fetched and report full-serve vs
  stub.
- **gr345270** — the dev-stdio launcher preflight runs `precis --help`
  then `precis serve`, duplicating the import chain over the bind mount
  (~2 s + 6 s unloaded). Moot once td458385 retires the per-session stdio
  containers; close rather than fix if that lands first.

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
