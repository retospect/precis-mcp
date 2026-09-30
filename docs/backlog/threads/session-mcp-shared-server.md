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
What remains is the migration itself: Reto decided on 2026-09-30 that the
per-session stdio containers are retired rather than hardened and every
session moves to this server (td458385), which makes wedge detection a
prerequisite rather than a follow-up. Then the parent item's remaining
criteria and the isolation gaps.
**Last reviewed:** 2026-09-30
**Worktree:** `session-mcp-shared-server`

## Do next
1. **td458385 — retire the per-session stdio containers; move sessions to
   the shared server.** Reto decided this on 2026-09-30, asked as the fork
   "supervisor, or accept staleness": *"supervisor and watchdog, and we move
   to HTTP."* It supersedes the two-step arm-then-restart plan this thread
   carried, and it supersedes
   backlog/mcp-staleness-title-roundtrip-guards.md item 2, which closes by
   removal — the population that item hardens stops existing.

   The measurement that forced the decision: supervision cannot work while
   the containers are stdio. Each one is `docker run -i --rm`
   (`AutoRemove=true`, `Restart=no`) and its stdin is the pipe of the client
   that spawned it, so a supervised restart produces a healthy container
   nobody is talking to — and `docker restart` on an autoremove container
   deletes it rather than restarting it. Arming `PRECIS_CHECKOUT_WATCHDOG`
   on them first would have been actively worse: `/app` is a read-only bind
   of main's *working tree*, so the 5 s arm exits on every qland, which with
   no supervisor is every session's MCP dying every few minutes during a
   burst.

   On HTTP none of that applies: the server stops being per-session, clients
   reconnect over a socket instead of owning a pipe, and the ensure script's
   `--restart unless-stopped` plus the already-armed watchdog are the whole
   mechanism. Work is the config migration plus letting the stdio containers
   die with their sessions; `--rm` disposes of each one.

   Ordering constraint, and it is the reason this is not purely a config
   edit: migrating the last session makes
   backlog/mcp-shared-server-liveness.md load-bearing rather than adjacent.
   One shared server for every session with no wedge detection is a single
   point of failure the migration *creates* — `--restart unless-stopped`
   acts on exit, never on a process that is up, listening and wedged. That
   item should land before or with the last session's move.

2. **backlog/mcp-shared-server-liveness.md** — promoted out of Horizon by
   td458385: it was a cost to watch while sessions still had their own
   containers, and the migration makes it a prerequisite. Nothing detects a
   server that is up, listening and wedged — the exact state
   `install_watchdog`'s docstring names, where the process "doesn't fail
   fast" but "desyncs at the protocol level ... and then hangs until the
   client's 1800 s idle timeout". `--restart unless-stopped` acts on exit
   and never fires on it, and there is no `HEALTHCHECK`. Under stdio a wedge
   cost one session and the operator noticing *was* the detection; shared,
   it costs twelve at once and none of them owns the server. Land this
   before or with the last session's migration.
3. **gr457326 follow-up: retry per batch, not per pass — and re-arm.**
   Dogfooding the landed fix on the shared server found the retry is at the
   wrong granularity, which matters more than the sleep length. Measured
   14:17-14:23Z over four consecutive boots: every attempt dies on its
   *first* batch, and the exception unwinds the whole pass, so attempts 2-4
   re-enter and die on that same first batch. Two of the failures were
   `embedder at capacity (429 after queueing)` returned in **0.6 s** — a
   condition that clears in seconds — and the pass then slept 60 s and
   burned another attempt. A retryable 429 on batch 1 of 315 should back off
   *inside* the batch loop and carry on, not tear the pass down.
   The scale is why this is load-bearing: `/app` is 20137 blocks (6.1 MB of
   text, p50 166 chars, max 71617), so `batch_size=64` is **315 sequential
   round trips**, not a handful. Progress is monotonic and does survive a
   bounce — the cache lives in the container's writable layer
   (`/home/precis/.cache/precis/md-vectors/bge-m3-1024.npz`), which the
   watchdog's process-exit does not clear — but it has produced ~256 of 20137
   vectors, and that file's mtime has not moved since 12:43Z: in 1 h 36 min
   of retrying, not one batch has landed.
   Not a capacity problem at the endpoint. Probed directly, 64 texts embed in
   7.8-8.9 s at 2-8 KB each, from the host and from inside the container
   alike, well inside the 15 s interactive budget. The contention is the
   twelve containers (this one plus eleven `precis-mcp-dev-*`) each
   boot-warming the same 20137 blocks of the same tree against one embedder
   with `max_inflight=4`.
   So three things, in order of how much they buy: (a) retry retryable
   errors per batch, honouring the 429's own retry-after, so a pass makes
   progress instead of restarting; (b) re-arm the pass later — on the first
   md search against a cold cache, or a periodic tick — since the watchdog
   bounces the process every few minutes during a qland burst and the pass
   never gets a contiguous window (it was abandoned mid-attempt-4 at
   14:22Z); (c) stop twelve containers racing to compute identical vectors,
   which is a shared-cache question, not a retry question.
   The instrumentation itself verified clean: `precis-status` over 8765
   reads `git_source watched-checkout`, `source_drift none`, and
   `md_vector_warmup retrying after attempt 1/4 (EmbedderUnavailable)`,
   which is how all of the above was observed rather than guessed.
4. **backlog/session-mcp-http-server.md** — AC2 passes now: it was written
   as "precis-status reports the new sha", which gr457361 made unpassable,
   and the 11:10Z banner
   (`precis-mcp 8.35.1 @ 05ce7657ceef (main) [watched-checkout] /src`)
   satisfies it. AC3 (a new verb kwarg surviving a bounce) was waiting on
   gr457887, which has landed and re-verified, so it is attemptable. Delete
   the item when AC3 and AC5 close.
5. **gr458350** — the `precis-mcp-dev-*` launcher passes prod credentials
   to `docker run` as `-e` values read from the password directory, so
   `docker inspect` prints the `agent_rw` DSN and four API keys in
   cleartext; hit while inspecting a container for this thread's work.
   Adjacent rather than owned here, but anyone debugging these containers
   walks into it — the gripe carries the names-only inspect format. Reto
   deferred rotation on 2026-09-30 ("later"); the leak path is the half
   that can be fixed without a rotation window, and td458385 shrinks it
   from twelve launchers to one.
6. **backlog/mcp-shared-transport-concurrency.md** — the gaps the shared
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

## Parked

- **backlog/session-mcp-http-server.md AC5** (one container after a day) —
  Reto ruled on 2026-09-30 that the stale dev containers get fixed at the
  next natural break, which lifts the no-sweep hold. It is satisfied by a
  reconnect wave, not a sweep: each session reconnects its own MCP, `--rm`
  disposes of the old container, and the fresh one imports current main.
  This item unparks once that wave has run and the fleet is verified —
  the check this thread owns. td458385 then closes it outright: with
  sessions on HTTP there is no per-session container left to count.
  `precis-mcp-http` is never touched.
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
