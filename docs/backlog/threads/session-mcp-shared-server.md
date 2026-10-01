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
makes it the single point of failure for every session. Since 2026-10-01
14:20Z a PID-1 supervisor holds its port across restarts, so restarts no
longer strand sessions (gr459481), and kills a wedged server (liveness
detector). Since 2026-10-01 ~17:52Z it serves the deployed code: `/src`
is a plain clone (`~/work/projects/code/precis-mcp-prod`) that
`scripts/deploy` moves to each deployed sha, so a qland no longer restarts
it and a deploy restarts it once (`deploy`'s change, Reto ran the
recreate). Embedder admission is answered (gr459844); next are the
isolation gaps.
**Last reviewed:** 2026-09-30 (pillar review same day added gr345270 and a
server-side-session-context Horizon pointer)
**Worktree:** `session-mcp-shared-server`

## Do next
1. **backlog/embedder-capacity-ownership.md — admission answered; owner,
   capacity number and shared cache left.** gr459088 and gr457326 are CLOSED, verified on the shared
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

   Admission is answered (gr459844), in three places:
   - The embedder service shares forward passes across requests,
     query-sized requests first, under a padded-token budget. On a rig with
     batches queued, query embeds went from 27 of 31 timing out to 0 of
     125, p50 ~2 s, with the same vectors.
   - The MCP process's own bulkhead has separate query and batch pools.
   - The skill index builds once, on a background thread, with failed
     skills retried after 60 s. That build was the 565-592 s call: every
     concurrent first skill search ran its own full build inline.

   A fresh cold server under the 12-session burst: 464 calls, 0 errors,
   max 8.4 s (592 s before). 78 query embeds still fell back to lexical,
   which is 12 sessions against 4 query slots doing their job. What is
   left in the item is the owner, the fleet capacity number and Reto's
   shared-cache call. A cold server's skill index completes in tens of
   minutes while md warm-ups saturate the embedder, which is that
   capacity question. The completed warm (~19400 vectors, 79 MB) exposed
   `add()`'s per-vector `np.vstack` as O(n^2); now a capacity-doubling
   buffer behind `_rows()`.

2. **backlog/session-mcp-http-server.md** — AC2 passes now: it was written
   as "precis-status reports the new sha", which gr457361 made unpassable,
   and the 11:10Z banner
   (`precis-mcp 8.35.1 @ 05ce7657ceef (main) [watched-checkout] /src`)
   satisfies it. AC5 passes as of 2026-10-01T11:00Z (one container). AC1
   passes since gr459481's supervisor (live 14:20Z 2026-10-01). Left: AC3,
   which closes opportunistically on the next verb-signature change someone
   else lands; delete the item when it does.
3. **gr458350** — prod credentials passed to `docker run` as `-e` values,
   so `docker inspect` prints them in cleartext. The precis half is in:
   with `PRECIS_SECRETS_FILE_DIR` set, the DSN and the MCP token are read
   from files there (`precis.secrets.mounted_secret`), and the API keys
   already were, through `get_secret`'s file layer. The secret-read
   guard now refuses `docker inspect` without a narrowing `--format`,
   `docker exec <c> env` and `/proc/<pid>/environ`. Left: the
   ensure script outside the repo
   (`~/work/infrastructure/precis-mcp/scripts/precis-mcp-http-ensure.sh`).
   The staged replacement (`precis-mcp-http-ensure.sh.gr458350-staged`,
   same directory) writes the container's secrets to
   `~/.cache/precis-mcp-http/secrets` (mode 700), mounts it read-only at
   `/run/precis-secrets` and drops every secret `-e`. It is not mounted at
   `/secrets`, because `docker/docker-entrypoint.sh` exports every file
   there as an env var. Rig-verified 2026-10-01 on 8768: no secret names in
   `.Config.Env` or the server's `/proc` env; DB, Wolfram and token auth
   all work. **Install only after a deploy carries the precis half** — the
   shared server serves the deployed clone, and the old code can't find
   the DSN in a file. Installing it recreates the container at the next
   SessionStart (the env hash covers the script's bytes), and auto mode
   denies that to agents, so Reto or `deploy` copies it in. Diff it against
   the live script first: it was cut from the 17:47Z version. Rotation is
   still deferred ("later", Reto 2026-09-30).
4. **backlog/mcp-shared-transport-concurrency.md** — the gaps the shared
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
