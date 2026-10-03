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
**Last reviewed:** 2026-10-03 (handoff: Waiting-on block added)
**Worktree:** `session-mcp-shared-server`

## Do next
**Waiting on the orchestrator or Reto (2026-10-03).** Do not rebuild these;
check their state first.
- **Branch `mcp-http-proxy` @ bbe473c46** (organizer-mcp-1): an always-up
  Caddy proxy plus blue-green backends, which supersedes Do-next 0's
  refused windows. The orchestrator gates it. Reto does the cutover per
  machine; the steps are in `deploy/mcp-http/README.md` on the branch.
  Tell the orchestrator the minute before.
- **Branch `pgbouncer-reset-readonly` @ 5fcb38b71** (organizer-pgbouncer-1):
  `track_extra_parameters`. Held for Reto. DISCARD ALL waits on gr463966
  and gr463967.
- **gr463517** (structural review ran without precis): the fail-closed
  MCP gate is qlanded as eebbb9a9f, not deployed. After the deploy, close
  the gripe on a clean structural pass. Step 3, an image rebuild with a
  current CLI, belongs to the orchestrator or Reto (review item
  session-mcp-shared-server-8).
- **After the round-2 deploy:** check that the drained respawn loads the
  gr462133 supervisor fix and fairness (c43ddf046), then close gr462133.

0. **gr460711 — any refused window longer than the client's retry budget
   strands sessions; make the server's refused windows short.** Measured
   2026-10-02 on Claude Code v2.1.285 (MCP client logs under
   `~/Library/Caches/claude-cli-nodejs/<project>/mcp-logs-precis/`):
   - **A connected session whose connection drops** makes 5 reconnect
     attempts at 0/1/2/4/8 s, ~15 s in all, then logs "Max reconnection
     attempts (5) reached, giving up" and never retries. Reproduced on
     the rig: port down 25 s → gave up at 15 s; `/mcp` → precis →
     Reconnect restores it.
   - **A session that starts while the port is refused** retries 3 times
     at 1/2/4 s, ~7 s, then gives up for its whole life. The orchestrator
     started 11:34:21Z while melchior was still rebooting (server up
     11:42Z) and never had precis. MCP connects run concurrently with
     SessionStart hooks, so the ensure hook cannot cover it;
     `scripts/fleet up` now waits for an HTTP answer before creating
     windows (d82bb08f7). The seat that runs `/fleet` started earlier and
     must check its own `/mcp`.
   - **A dropped GET stream whose port is not refused** (a watchdog
     bounce, 10:54Z) exhausts its own 2-attempt SSE budget and keeps the
     transport up — POST still works. That is why respawns never
     stranded anyone.
   The 10-01 stranding fits the first point: a hard `docker rm -f`
   recreate holds the port refused from the kill until the new
   supervisor binds. It is consistent, not proven, since the 10-01 client
   logs were not read. The server-side fix is fewer and shorter refused
   windows, shipped as ONE ensure-script install (an edit is itself a
   recreate). It is staged at
   `~/.claude/projects/-Users-reto-precis-mcp/scratch/gr460711/ensure.sh`
   on melchior:
   - secret rotations SIGHUP the child instead of recreating;
   - the label hashes the container spec, not the script file;
   - recreate is create → `docker stop -t 45` (drains via the
     supervisor's SIGTERM→SIGHUP, 8ebb9d785) → start, under
     `trap '' HUP INT TERM`, so a killed hook cannot leave the port dark;
   - `follow_prod` keeps the served clone on origin/prod (deploys move it
     only on the deploying machine; melchior served 06e3f3d7 under a
     0dc5e6a0 prod), at most one fetch a minute, at SessionStart;
   - the prepare step syncs new or bumped dependencies from `uv.lock`
     (a failed dry-run no longer stamps the lock) and reinstalls project
     metadata when `pyproject.toml` changes. It logs per-phase seconds
     and has uv's cache on a named volume — not a host bind, whose file
     count on virtiofs is the panic risk.
   Rig, staged script, 2026-10-02:
   - idle recreate: refused 0.34 s;
   - recreate with a 9 s search running on the old container: the stop
     drained it, the interactive client got its answer, refused 0.43 s,
     and its next call worked;
   - fresh-container prepare: 6 s on an idle host (copy 2 / deps 3 /
     metadata 1). The 40 s seen at 14:40Z was under six gate workers;
     time it again on the real install.
   - a client idle ~2 h (last call before 14:59:27Z) went through a
     recreate at 17:01Z, and its next tool call reached the new server.
   **Installed on melchior 2026-10-02 ~20:56Z** (Reto ran the `cp` and the
   `--recreate`; a session's `cp` was denied by the permission
   classifier):
   - port 8765 refused for under a second (forwarder stop and start both
     at 20:56:42Z);
   - supervisor bound at 20:56:43Z; prepare took 7 s (copy 2, deps 4,
     metadata 1);
   - generation 1 started 20:56:50Z, serving 567f207fd with no source
     drift.
   This first stop ran through the pre-drain supervisor; drains apply
   from the next recreate. The orchestrator's sweep counts the drops
   against a before-state of 5 DOWN.
   **Still to do:**
   - Reto's dev-Mac session installs the same script; keep the scratch
     copy until then.
   - The ensure script is under no version control:
     `~/work/infrastructure/precis-mcp/` is not a git repo, and its only
     history is the scratch copies. Its live/test guard therefore has no
     CI test (gr462596 diagnosis). The candidate home is
     `scripts/precis-mcp-http-ensure.sh` here, with the SessionStart hook
     pointing at the checkout's copy, but that entangles with Horizon 1
     (mcps role vs hand-rolled wrapper). Decide with that item.
   **gr462133 (round-1 gate hang):** the supervisor swallowed a stop
   SIGTERM that landed between reaping one generation and assigning the
   next. Fixed in 7f006bf09 (round 2); it loads at the first recreate
   after the deploy that carries it.
   **Incident gr462598, 2026-10-02 15:04:13–15:04:54Z (this thread):** a test
   harness for the fleet wait ran the live ensure script with test ports;
   it removed the server, then recreated it on 8799. Restored on 8765 by
   hand. 8765 was refused for 24 s, so every connected session on melchior
   gave up at 15:04:29Z and needed `/mcp`. `wait_mcp` now takes
   `PRECIS_MCP_ENSURE`, and its test never reaches the real script.
1. **backlog/embedder-capacity-ownership.md — decided, handed to
   local-compute.** Reto ruled 2026-10-02 (item -4, td461158 closed):
   - the embedder service owns aggregate capacity, held by local-compute;
   - a provisional capacity number is recorded;
   - no host-level admission token, no shared vector cache.
   The N-client load test waits for the local LLM rungs. Root cause and
   the admission fix (gr459844) are in the item.

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
   image rebuild bounces every session. The role bullet is closed (Reto,
   item -5, td461159): coding jobs never leave containers and the shared
   server stays interactive-only at `agent_rw`. Its reopen trigger is in
   Parked. Fairness is built: permits go round-robin by session
   (`server._FairSemaphore`). It loads at the next drained respawn after
   a deploy. The pool-storm test is built (`tests/test_mcp_pool_storm.py`).
   Left: the supervision acceptance bullet, then delete the item.

## Runbook

- **A deploy that changes `[project.entry-points]` or `uv.lock`** needs
  no hand step on melchior since the 2026-10-02 install. The watchdog
  bounce re-runs the prepare step, which reinstalls project metadata when
  `pyproject.toml` changed and syncs new or bumped dependencies; check
  the `precis-prepare:` lines in `docker logs`. A dependency with no
  wheel, or an autocatpath bump, still needs an image rebuild.
- **Installing a staged ensure script:** the auto-mode permission
  classifier denies a session copying over the live script ("Modify
  Shared Resources"). Reto runs the `cp`; the session prepares the
  staged file, diffs it, and verifies the result. Tell the orchestrator
  before any `--recreate`, so it can count drops and sweep.
- **A rig never uses the live name:** use scratch `rig*.sh` with their
  own `PRECIS_MCP_HTTP_NAME`. The installed script refuses
  PORT/STATE/IMAGE/SRC overrides on `precis-mcp-http` unless
  `PRECIS_MCP_LIVE=1` (gr462596).

## Horizon

1. **backlog/mcps-venv-deploy-gaps.md** — the shared server is a hand-rolled
   dev-machine service by decision (Reto, 2026-09-29); whether the fleet
   mcps role adopts this shape decides if the ensure script stays a wrapper
   or becomes an Ansible role. Until then the two must not entangle.
2. **backlog/mcp-shared-server-multiprocess.md** — `--workers 1` is correct
   and is the ceiling; the inventory of what must leave process memory
   first. Arc, not work: 12 concurrent searches finish in 4.78 s against a
   2.06 s single call.
3. **K-parallel exercise-mcp on dev** (unfiled) — the acceptance gate for
   fairness and the pool sizing; before fairness deploys it measures an
   idle server.
4. **pgbouncer cl_waiting observability** (td458386) — granted by Reto
   2026-09-30, not yet wired; prerequisite for every future claim about
   pool headroom, because without it entry 3 cannot tell "pool starved"
   from "something else slow".
5. **backlog/server-side-session-context.md** — owned by
   graph-memory-consumers, not this thread; pointer only. Precondition
   td458385 (sessions moving to this server) is this thread's own
   Do-next 1.
6. **backlog/mcp-verb-kwarg-parity.md** — 71 handler kwargs are silently
   dropped by put/edit; the verb signature is the MCP schema, so a dropped
   kwarg is a silent no-op for every session. Platform pass 2026-10-02.
7. **backlog/singleton-id-no-batch-form.md** — numeric-ref verbs take one
   id; `id=[...]` crashes instead of batching.
8. **backlog/gripe-comment-timeline-uncapped.md** — a bare get on a gripe
    renders every comment; unbounded response on the shared server.
9. **backlog/perplexity-block-handle-guard.md** — get on a perplexity kind
    with a search block handle cost ~$0.50; a spend guard on the surface.
10. **backlog/time-kind.md** — stateless time/date kind like calc; no
    handler in src, still open.
11. **backlog/mcp-staleness-title-roundtrip-guards.md** — title round-trip
    assert plus an MCP staleness banner; guards the stale-process class.
12. **backlog/improve-ack-scrape-eradication.md** — replace regex-on-ack
    with structured Response fields.
13. **backlog/cli-bind-store-audit.md** — CLI entrypoints without bind_store
    miss live routing.
14. **backlog/serverinfo-title.md** — serverInfo.title blocked upstream on
    FastMCP; still blocked, so last.

## Parked

- **Role isolation on the shared server** (Reto 2026-10-02, item -5) —
  closed as "coding jobs never leave containers; the shared server stays
  interactive-only at agent_rw". Unparks when a judge/extract job class
  declares `write:none` AND per-container serve boot shows up as a
  measured cost; then build per-role pools keyed on the bearer token
  (backlog/mcp-shared-transport-concurrency.md).

- **pgbouncer admin-console read access** (td458386) — **granted by Reto
  2026-09-30**; unparks when the coordinates are in the overlay and a
  `SHOW POOLS` read works from a script. cl_waiting is the only direct
  evidence of pool starvation, so until it lands the 17/100-connections
  reading and every other pool-headroom statement stay inference from
  backend counts and should be re-derived, not inherited.
- **K-parallel exercise-mcp load harness** — specced inside
  backlog/mcp-shared-transport-concurrency.md; the role gap is decided and
  fairness is built; unparks once the round-2 deploy loads fairness.
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
