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
recreate). Embedder admission is answered (gr459844). Since 2026-10-01
20:25Z secrets reach it as mounted files, not container env (gr458350), and
since 23:13Z its caches live on a host mount that survives a recreate
(gr460339). Next: stop recreates stranding interactive sessions, then the
capacity and isolation gaps.
**Last reviewed:** 2026-10-02 (handoff: Do-next 0 + Runbook added)
**Worktree:** `session-mcp-shared-server`

## Do next
0. **gr460711 — a container recreate strands sessions; a supervisor respawn
   does not.** The two `--recreate` runs at 2026-10-01 23:13Z (cache mount,
   gr460339) left several sessions without the MCP until a manual /mcp, and
   the four watchdog respawns before and after stranded nobody. A recreate
   is `docker rm -f`: SIGKILL, open streams reset, the port dark ~9 s until
   the new supervisor binds. It ranks first because every ensure-script edit
   and every secret rotation triggers it at the next SessionStart anywhere.
   Rig, 2026-10-02 (copy of the ensure script on 8767, real
   `claude -p` client): one recreate, two recreates ~55 s apart, and a call
   issued ~3 s into the dark window all re-initialized cleanly. So the
   non-interactive client survives, and the stranding is specific to
   long-lived interactive sessions — this session reconnected after both
   prod recreates, deploy's and twinkly's did not. Leading hypothesis: the
   interactive client does not reset its reconnect budget after a
   successful reconnect, so a second drop soon after the first exhausts it.
   That is client-side, so the server-side fix is fewer and shorter drops,
   shipped as ONE ensure-script install (an edit is itself a recreate):
   (a) a secret rotation rewrites the mounted files and respawns the child
   instead of recreating; (b) `--recreate` uses `docker stop -t` and
   drains; (c) the prepare step reinstalls `--no-deps -e /app` when
   `pyproject.toml` differs from a stamp in the venv (see Runbook);
   optionally (d) a holder container owns the port's network namespace.
   Proving the hypothesis needs two interactive windows on the rig (a
   human, or `claude` driven through tmux).
   **Progress 2026-10-02 (melchior):** the rig showed SIGTERM cannot drain
   — uvicorn's shutdown cancels the session manager's task group, so the
   in-flight call returned an empty body. Landed in the repo: SIGHUP on a
   supervised serve child runs the watchdog's high-water drain and exits 0
   (`install_watchdog.install_drain_signal`), the supervisor forwards
   `docker stop`'s SIGTERM as SIGHUP, and a child killed by SIGTERM/SIGHUP
   is a restart, not a crash. Rig, on the worktree code: an in-flight 10 s
   search survived both a SIGHUP respawn and a `docker stop` recreate.
   The ensure-script half — (a) secrets hashed apart and respawned via
   SIGHUP, (b) create-then-stop recreate, (c) the pyproject stamp, plus
   the label hashing the container spec instead of the script file — is
   staged at `~/.claude/projects/-Users-reto-precis-mcp/scratch/gr460711/ensure.sh`
   on melchior and rig-verified; install it after the deploy that carries
   the supervisor change, in one recreate.
   **Second gap, same day:** `scripts/deploy` moves the prod clone only on
   the machine that runs it, so melchior's clone (made 10:01Z) never moved
   while deploys ran elsewhere — the server served 06e3f3d7 under a
   0dc5e6a0 prod. Moved by hand 10:54Z; the ensure script must follow
   origin/prod itself.
1. **backlog/embedder-capacity-ownership.md — admission answered; owner,
   capacity number and shared cache left — Reto's call, td461158.** gr459088 and gr457326 are CLOSED, verified on the shared
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
3. **backlog/mcp-shared-transport-concurrency.md** — the gaps the shared
   process opens: one DB role for every session (measured: no
   PRECIS_MCP_DB_ROLE/_ENFORCE, DSN user agent_rw), no fairness on a
   first-come semaphore, no supervision for a single point of failure whose
   image rebuild bounces every session. Ranked last because its acceptance
   criteria are verified by probing the surface they fix — that surface is
   now trustworthy, so this is unblocked rather than waiting. The role
   bullet decides whether coding jobs can ever leave containers; Reto's
   call, td461159.

## Runbook

- **A deploy that changes `[project.entry-points]`** (every plugin-split
  extraction): the image's venv is a path-editable install (pth →
  `/app/src`), so new modules import but entry points stay at the image
  build's — e.g. `precis.skills` missing, and the moved skill is NotFound.
  After the deploy moves the prod clone, refresh in place:
  `docker exec precis-mcp-http uv pip install --python /opt/venv/bin/python
  --no-deps -e /app`, then SIGTERM the serve child (pid from the
  supervisor's "generation N started (pid X)" log line) so the supervisor
  respawns it — never `--recreate` for this (gr460711). Any later recreate
  reverts it to the image's metadata until Do-next 0 (c) lands.
- **Installing a staged ensure script** is the agent's job after a deploy
  (Reto 2026-10-01): diff, cp, `--recreate`, verify with the names-only
  `docker inspect` format and precis-status, and have deploy send one ping
  covering every restart.

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
8. **backlog/mcp-verb-kwarg-parity.md** — 71 handler kwargs are silently
   dropped by put/edit; the verb signature is the MCP schema, so a dropped
   kwarg is a silent no-op for every session. Platform pass 2026-10-02.
9. **backlog/singleton-id-no-batch-form.md** — numeric-ref verbs take one
   id; `id=[...]` crashes instead of batching.
10. **backlog/gripe-comment-timeline-uncapped.md** — a bare get on a gripe
    renders every comment; unbounded response on the shared server.
11. **backlog/perplexity-block-handle-guard.md** — get on a perplexity kind
    with a search block handle cost ~$0.50; a spend guard on the surface.
12. **backlog/time-kind.md** — stateless time/date kind like calc; no
    handler in src, still open.
13. **backlog/mcp-staleness-title-roundtrip-guards.md** — title round-trip
    assert plus an MCP staleness banner; guards the stale-process class.
14. **backlog/improve-ack-scrape-eradication.md** — replace regex-on-ack
    with structured Response fields.
15. **backlog/cli-bind-store-audit.md** — CLI entrypoints without bind_store
    miss live routing.
16. **backlog/serverinfo-title.md** — serverInfo.title blocked upstream on
    FastMCP; still blocked, so last.

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
