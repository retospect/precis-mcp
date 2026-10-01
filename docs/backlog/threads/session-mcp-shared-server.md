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
What remains is the migration itself: Reto decided on 2026-09-30 that the
per-session stdio containers are retired rather than hardened and every
session moves to this server (td458385), which makes wedge detection a
prerequisite rather than a follow-up. Then the parent item's remaining
criteria and the isolation gaps.
**Last reviewed:** 2026-09-30 (pillar review same day added gr345270 and a
server-side-session-context Horizon pointer)
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
   mechanism.

   **There is no config edit outstanding — the flip happened 2026-09-29
   around 17:00.** `~/.claude.json` already carries one top-level
   `mcpServers.precis` of `type: http` pointing at `127.0.0.1:8765/mcp`,
   with no project-level override, and the newest `precis-mcp-dev-*`
   container dates from 09-29 16:54 with none since despite 15 live
   sessions — a session reading a stdio command would have made one. The
   remaining containers belong to sessions that connected before the flip
   and are still holding that connection; config is read at connect time,
   so `/mcp` → `precis` → reconnect (or ending the session) is the whole
   fix, and `--rm` disposes of the container. The reconnect wave is not a
   stopgap before the migration, it *is* the migration.

   Wedge detection (Do-next 2) follows rather than gates, decided
   2026-09-30 when the pillar review asked: 15 sessions are already on the
   shared server, so holding the wave protects nobody and only keeps
   eleven on stale code while three thread owners wait. A wedge is
   recoverable by hand (`precis-mcp-http-ensure.sh --recreate`) and
   `scripts/prod-precis tools ...` is the fallback for a dead MCP; what is
   missing is detection, not recovery. Measured floor for any probe:
   a bounce takes ~8 s end to end (exit 23:14:30.5Z → armed 23:14:38.8Z,
   and three more bounces within 9 s of each other), so a liveness check
   must tolerate an 8 s gap or it will restart a server that is merely
   restarting. Revisit only if a wedge lands
   first — that would make it a measurement rather than a projection.

2. **backlog/mcp-shared-server-liveness.md** — promoted out of Horizon by
   td458385: it was a cost to watch while sessions still had their own
   containers, and the migration makes it the next thing that matters
   (follows the wave rather than gating it — see Do-next 1). Nothing detects a
   server that is up, listening and wedged — the exact state
   `install_watchdog`'s docstring names, where the process "doesn't fail
   fast" but "desyncs at the protocol level ... and then hangs until the
   client's 1800 s idle timeout". `--restart unless-stopped` acts on exit
   and never fires on it, and there is no `HEALTHCHECK`. Under stdio a wedge
   cost one session and the operator noticing *was* the detection; shared,
   it costs twelve at once and none of them owns the server.
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
   This item is now only the per-call half, and (a) has LANDED — Reto's
   go 2026-09-30. `embed_missing` grew an `on_batch_error(exc, i, n)`
   callback and `server._warm` owns the policy: only `EmbedderUnavailable`
   retries, in place, honouring the service's `retry_after_s`, 6 attempts
   per batch on a 2s ladder capped at 60s; a `ValueError` still fails the
   pass on first sight. The policy had to live in `server.py` because
   `md_index` deliberately does not import `precis.embedder`
   (`vectors.py::MdEmbedder`) — the callback is that seam.
   Dogfooded 22:18-22:21Z on the shared server: the new shape is live and
   correct (`batch 1/297 ... retry 1/6 in 2.0s` through `5/6 in 32.0s`,
   then one COLD, no whole-pass unwinding). It still does not warm, and
   the cause turned out not to be in this thread's code at all —
   **gr458940**: the embedder service on 8181 has run since 2026-08-31,
   so gr450123's bounded wait queue (943aba15, 2026-09-28) has never
   executed. `/metrics` is missing the three `queued*` counters current
   code emits, the 429s carry no `retry_after_s`, and a direct probe of
   the same endpoint served 64 texts in 9-11s three times running with no
   429. It sheds instantly instead of queueing. ⚠ This retires the
   premise `backlog/embedder-capacity-ownership.md` was built on: that
   item cites our dogfood as proof "(b) proves insufficient", which is
   what trips gr450123's deferral of host-level admission (a). (b) never
   ran. Re-decide after the restart, and do not tune the batch retry
   budget against a stale daemon.
   (b) has LANDED too (Reto's go, 2026-10-01). `MdHandler.search` calls
   `self.rearm_warmup()` when `indexed_blocks < total_blocks`; the hook
   is installed by `server._warm_md_index_background`, which owns a
   non-blocking single-flight guard plus a 60s cooldown. Trigger is a
   cold-cache search, not a timer — it costs nothing when nobody
   searches, fires exactly when someone is about to get degraded
   results, and a timer asleep for an hour is indistinguishable from the
   wedged thread this whole item is about. Motivated directly by the
   00:19Z measurement: the pass gave up while the embedder was busy and
   `inflight` was 0 three minutes later with nothing able to return.
   Verified live 01:20:15Z: a cold-cache `search(kind='md')` against the
   shared server started a pass 28 min after the boot pass went COLD.
   Dogfooding it immediately found one defect it introduced, now fixed:
   the cooldown was flat 60s, so a cold cache plus twelve searching
   sessions could start a pass a minute each against a service with one
   working slot — the warm pass amplifying the 429 storm starving it. It
   now doubles per consecutive failure (60s base, 900s cap, reset on
   success), which needed `_warm()` to return whether it completed.
   ⚠ And the capacity finding that bounds all of this (gr459088 comment
   2): `inflight` never returns below **3 of 4** over ~8 min and was 0 at
   00:32Z, while a single-text embed is refused in 0.000s. Three slots
   are held by calls that never finish; effective capacity is one slot.
   No schedule fixes that. The restart in gr458940 reclaims them, and
   will *look* like the bounded queue working — measure `inflight`'s
   floor afterwards before crediting the queue, and keep watching it: if
   the floor climbs from 0 again over hours, the leak is live in current
   code and is the real bug.
   Superseded, kept for the record: (b) re-arm the pass later — on the first md search
   against a cold cache, or a periodic tick — since the watchdog bounces the
   process every few minutes during a qland burst and the pass never gets a
   contiguous window (it was abandoned mid-attempt-4 at 14:22Z). Both stay
   here.
   The third thing — twelve containers racing to compute identical vectors —
   went up a layer to `backlog/embedder-capacity-ownership.md` (pillar
   local-compute, ranked in the dormant `threads/local-compute.md`), which
   holds both of the Reto calls: the shared-cache yes/no plus its
   multi-writer mechanism on the `.npz`, and whether a host-level admission
   token should now be built. Do not re-plan either from this thread — that
   item names our dogfood as the evidence that settles a prior deferral:
   gr450123 deferred host-level admission (its option (a)) "until (b) proves
   insufficient", and (b) is the bounded wait queue, which targets the
   interactive request path — not a boot-warm storm. It also lists
   `md_index/vectors.py` in its blast radius, so coordinate before touching
   the cache file format.
   The instrumentation itself verified clean: `precis-status` over 8765
   reads `git_source watched-checkout`, `source_drift none`, and
   `md_vector_warmup retrying after attempt 1/4 (EmbedderUnavailable)`,
   which is how all of the above was observed rather than guessed.
3. **gr459088 — the embedder sheds continuously; gr457326's retry and
   re-arm are both working and both futile.** Dogfooded on prod
   2026-10-01 01:20-01:25Z. rustling's re-arm (2b729531) does fire — seven
   seconds after a cold-cache `search(kind='md')` the log shows `warming md
   index vector cache for 1 root(s)` — and then fails the same way the two
   boot passes did, always on **batch 1 of 299**, so progress is zero
   rather than partial. A live md search says so itself:
   `semantic: 1% of blocks indexed`.

   The cause is not retry policy. Calling `http://127.0.0.1:8181/embed`
   directly, bypassing precis: four of five attempts are refused at the
   admission gate with `429 {"error":"busy"}` in under a millisecond, and
   the fifth is admitted and does not return within 20 s. The service is up
   and answering — it has nothing free to answer with. Sixty-two seconds of
   backoff against a service shedding for forty minutes buys nothing, and
   no schedule short of hours would.

   So this item is no longer "re-arm the warm pass": that landed and is
   verified. It is capacity, or a warm pass that yields to request-path
   traffic instead of racing 15 sessions for the same four slots. Open
   question in the gripe: whether the one admitted request taking >20 s is
   normal under load or a slot held by something that never completes —
   capacity lost rather than exhausted.

   ANSWERED (my measurement, gr459088 comment 2): it is a slot held by
   calls that never complete — capacity lost, not merely exhausted.
   `inflight` never returns below **3 of 4** across ~8 min of polling with
   zero load from me, and it was **0** at 00:32Z today; one slot still
   cycles 3-4, so the release path works. A single-text embed into that
   state is refused in **0.000s** with `{"error":"busy"}` while `inflight`
   reads 4. Effective capacity is one slot, which is why no schedule helps.
   The gr458940 restart reclaims the three stuck slots as well as picking
   up the never-run bounded queue, so it will *look* like the queue fixed
   things. Measure `inflight`'s floor afterwards before crediting the
   queue, and keep watching it — a floor that climbs from 0 again over
   hours means the leak is live in current code and is the real bug.
   Which call leaks is unestablished and needs the service's own request
   logging; that is embedder-service territory, not md_index.

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
7. **backlog/server-side-session-context.md** — owned by
   graph-memory-consumers, not this thread; pointer only. Precondition
   td458385 (sessions moving to this server) is this thread's own
   Do-next 1.

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
