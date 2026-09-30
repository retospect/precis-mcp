---
status: in-progress
title: Session precis MCP becomes one long-lived local streamable-http server, sized for 12 concurrent sessions
prio: high
model: opus
---

# Session precis MCP becomes one long-lived local streamable-http server

## Motivation / why

The session MCP is a per-session stdio container
(`~/work/infrastructure/precis-mcp/scripts/precis-mcp-dev-stdio.sh`:
`docker run -i --rm --name precis-mcp-dev-$$`), bind-mounting the main
checkout read-only at `/app`. Two defects compound:

1. **Its source is rewritten underneath it.** `scripts/ship` (which `/go`
   runs before `scripts/deploy`) resets the main checkout. Every live
   container's `/app` changes mid-session; already-imported modules stay
   loaded, lazily-imported ones come from the new tree.
2. **The recovery path built for exactly this is disabled here.**
   `precis.install_watchdog` exists because "a live server whose
   site-packages are swapped underneath it doesn't fail fast — it desyncs
   at the protocol level ... and then hangs until the client's 1800 s idle
   timeout". Its fix is a deliberate `os._exit(0)`, on the documented
   premise that "the MCP client ... restarts a fresh server on the next
   connection". But `_fingerprint_for` returns `None` outside
   `site-packages`, and the dev container imports from `/app/src/precis`
   (verified: `docker exec precis-mcp-dev-22292 python -c "import precis;
   print(precis.__file__)"`). So dev gets the wedge and not the recovery.

Observed: six `precis-mcp:dev` containers alive, 2 minutes to 5 days old,
zero `die` events in 72 h — they are not crashing, they are wedging.
Stdio makes this unrecoverable within a session because the client owns
the process; a wedged pipe has no reconnect.

Moving to streamable-http changes the failure from fatal to invisible:
the client holds no process, only an `Mcp-Session-Id`. A restarted server
does not recognise that id, returns 404, and the client re-runs
`initialize` — which also re-reads the tool list, closing the
`mcp_verb_kwarg_silent_drop` shape where a client on a stale tool list
silently drops a newly-added verb kwarg.

The server side already exists and is in production use:
`server.py::_run_network_transport` + `_install_token_auth`, exercised by
`workers/executors/_sandbox_read_mcp.py` for sandbox `precis_access:read`
runs. This item adds a *second caller* of that endpoint and the
lifecycle around it; it does not build a new transport.

Multi-session was already designed for — `precis/serve_ledger.py`:
"two concurrent agent sessions must never see each other's ledger, **and
the HTTP transport serves many sessions from one process**".

## In scope

**1. A long-lived container, lazily started by the first session that
wants it** (Reto, 2026-09-29 — no launchd, no Ansible role for now).
Under HTTP the client runs no command at all, so the start has to come
from somewhere else: a `SessionStart` hook calling an idempotent
`precis-mcp-http-ensure.sh`. The first session creates the container;
every later session's hook sees it running and no-ops. This is the
existing idiom on this machine — `reap-stale-serves`, `inflight` and
`memory-lint` already run from `SessionStart`.

Dropping launchd also drops `KeepAlive`, which the checkout watchdog
(item 3) depends on — its whole design is *exit cleanly and let something
restart me*. Docker's own restart policy takes that job:
`docker run -d --name precis-mcp-http --restart unless-stopped` (**not**
`--rm` — the two flags are mutually exclusive), serving
`precis serve --transport streamable-http --host 127.0.0.1 --port 8765`.
Port 8765 is `server.main`'s own default and is free on melchior (checked
against `lsof -iTCP -sTCP:LISTEN`).

A named, non-ephemeral container pins its env at *creation*, so changing
a sizing knob or the token needs a recreate rather than a restart. The
ensure-script stamps a hash of the env set as a container label and
recreates on mismatch — without this the knobs in the sizing table below
are not actually adjustable short of a manual `docker rm`.

Single uvicorn worker — **never** `--workers >1`: the serve ledger's
`WeakKeyDictionary`, the pagination-cursor registry (`_init_runtime`'s
`long_lived = True`, gr267466) and the DB pool are all per-process.

**2. Snapshot the source instead of bind-mounting it.** The container
gets `-v "${REPO}:/src:ro"` and its entrypoint copies `/src` to `/app`
(excluding `.git`) before serving. With one long-lived process the live
bind-mount is actively harmful — a half-applied checkout is a
half-consistent import tree. Cost is now amortised per *server* lifetime,
not per session. Repo is 95 MB including `.git`.

**3. A checkout watchdog, as a sibling mode of the install watchdog.**
Extend `install_watchdog.py` with a source-checkout arm, enabled by
`PRECIS_CHECKOUT_WATCHDOG=<path-to-source-root>`, fingerprinting the
resolved HEAD sha of that root (`_git_sha_short` already exists) —
**not** file mtimes, which is precisely why the install arm refuses
source checkouts ("every `git checkout` touches mtimes"). A sha-keyed
fingerprint fires on ship/sync/qland and stays silent on editor saves.
Poll interval 5 s (one `rev-parse`, cheaper than the 20 s install arm's
stat, and a bounce now costs all sessions at once so the half-applied
window must be short).

**4. Quiesce before exiting — via a thread-safe in-flight counter, NOT
the tool semaphore.** Unlike the install arm, this one exits while 12
sessions may have calls in flight, so `os._exit(0)` must wait for
dispatch to drain.

The obvious mechanism does not work and must not be attempted:
`_get_tool_semaphore()` returns an `anyio.Semaphore` whose `acquire()` is
`async def`, only ever awaited inside `async with sem:` on the FastMCP
event-loop thread (`server.py::_offload_sync`'s `wrapper`). The watchdog
is a plain `threading.Thread` with no event loop; calling `.acquire()`
there returns an unawaited coroutine — a silent no-op past a
`RuntimeWarning` nobody reads — and the exit proceeds anyway. Bridging
correctly would need an `anyio.from_thread` blocking portal handed over
from the async side.

Instead: a module-level `threading.Lock`-guarded in-flight counter plus a
`threading.Event`, incremented/decremented around the `to_thread` call in
`_offload_sync` and waited on by the watchdog with a bounded timeout.
Plain threading primitives are visible from both the event-loop thread
and the daemon thread, so no portal is needed. Timeout expiry exits
anyway — a wedged call must never block the bounce forever.

**5. Client config.** `~/.claude.json`'s `precis` entry becomes the shape
`_sandbox_read_mcp.py::mcp_json_payload` already writes and the sandbox
already consumes:

    {"type": "http",
     "url": "http://127.0.0.1:8765/mcp",
     "headers": {"Authorization": "Bearer <token>"}}

Token from `~/.secrets/pw/PRECIS_MCP_TOKEN` (new), read by the
ensure-script and pasted into the client config. `_install_token_auth` is a constant-time
bearer check and needs no change.

**6. Make the exit breadcrumb per-session, not per-process.**
`consume_last_exit_breadcrumb` deletes the file on first read, for the
documented invariant "a breadcrumb is surfaced exactly once, ever". Under
one process serving 12 sessions that regresses to *one of twelve* — the
other eleven get the gr341515 failure ("the server simply gone with zero
explanation") on every bounce. Fix: read-and-delete once at boot into a
module-level value, then serve it once **per MCP session**, keyed on the
session object exactly as `serve_ledger` already keys its own state. The
invariant becomes "exactly once per session", which is what it always
meant when a process had exactly one client.

**7. Sizing — see below.**

## Sizing for 12 concurrent sessions

Twelve sessions are mostly idle; the burst case is what must not queue.

| Knob | Today | Set to | Why |
|---|---|---|---|
| `PRECIS_MCP_TOOL_CONCURRENCY` | 4 (`_DEFAULT_TOOL_CONCURRENCY`) | **12** | One permit per session, so no session's `search` blocks behind another's. |
| DB pool `max_size` | 10 (`pool.py::DEFAULT_POOL_MAX_SIZE`) | **16** | Must stay above tool concurrency — `server.py`'s own rule for the semaphore is that the pool binds "only under genuinely pathological fan-out". 12 + 4 for background warmups (embedder, md-index) and heartbeat. |
| DB pool `min_size` | 2 | **4** | One shared server replaces 6 containers x 2 = 12 idle connections; 4 is a *reduction* in steady-state load on pgbouncer. |
| anyio to-thread limiter | ~40 default | unchanged | Already above 12. |
| uvicorn workers | 1 | **1, pinned** | Per-process state, above. |

pgbouncer headroom is fine and unchanged: `pool_mode = transaction`,
`default_pool_size = 25` per (user, db), `max_client_conn = 200`
(`deploy/roles/pgbouncer/templates/pgbouncer.ini.j2`). Sixteen `agent_rw`
client connections sit under the 25-connection server pool, and today's
six containers already draw ~12 idle. Role-level caps are live DB state,
not repo state, so this was checked rather than assumed — 2026-09-29,
`scripts/prod-psql "SELECT rolname, rolconnlimit FROM pg_roles ..."`:
`agent_rw` and `agent_ro` both `-1` (unlimited). `gate_precis_role_connlimit`
does not apply. Re-check if the 16-connection sizing ever starves without
`pool.get_stats()` showing a pool-side queue.

**New knobs required** — pool size is presently a code constant with no
env override (`store/store.py::Store.connect` /
`store/pool.py::create_pool` take it as a kwarg only). Add
`PRECIS_DB_POOL_MIN_SIZE` / `PRECIS_DB_POOL_MAX_SIZE` resolved in
`create_pool`'s defaults so the plist can set them without a code change.

## Explicitly NOT in scope

- **The trust boundary does not move.** This stays `agent_rw` against
  PROD, same as the current session MCP (CLAUDE.md "Session `precis` MCP
  targets PROD"). It does not adopt `_sandbox_read_mcp`'s `agent_ro`
  derivation — that is the sandbox's boundary, not this one.
- **No change to `precis serve`'s transport code.**
  `_run_network_transport` / `_install_token_auth` are used as-is.
- **`stateless_http` is deliberately NOT set.** FastMCP's default
  (session ids issued) is what produces the 404 then re-`initialize` then
  fresh tool list on restart. Stateless mode would make a restart
  invisible *and* leave every client on a stale tool list.
- **Not a prod change.** Cluster daemons keep their own stdio/venv path
  and the existing `site-packages` install watchdog. Only the developer
  session MCP on melchior moves.
- **No per-worktree source views.** All sessions share one `/app`
  snapshot of the main checkout — the same as today, where every
  container bind-mounts the same `REPO`.
- **No retry-on-restart, anywhere.** A call landing inside the ~2-5 s
  restart window fails and must be re-issued by hand. Decided (Reto,
  2026-09-29): the window is acceptable, so no retry in the client, the
  wrapper, or the server.
- **No stdio fallback is retained.** `precis-mcp-dev-stdio.sh` is deleted
  in the same change, not kept as a swap-back path. Decided (Reto,
  2026-09-29). Recovery from a broken HTTP server is
  `docker restart precis-mcp-http`, not a config swap.

## Acceptance criteria

0. **First-contact check, run before anything else is built.** Stand the
   container up by hand, point one session's `~/.claude.json` at it,
   `docker restart precis-mcp-http`, and issue a tool call. AC1-AC3 all
   rest on Claude Code's client treating a streamable-http 404 as
   "reconnect, re-`initialize`, refresh the tool list". The *server* half
   is confirmed against the pinned wheel (`mcp==1.28.1`'s
   `StreamableHTTPSessionManager` defaults `stateless=False` and returns
   404 on an unknown session id), but the client half is external to this
   repo and cannot be pre-verified. If it surfaces a hard error instead,
   the premise of the whole item is wrong and the build stops here.
1. `docker restart precis-mcp-http` while a Claude session is open: the
   next `precis` tool call in that session succeeds, with no `/mcp`
   reconnect and no Claude Code restart.
2. A `/go` (ship + deploy) run while two or more sessions are open: both
   sessions' next tool call succeeds and `get(kind='skill',
   id='precis-status')` reports the new sha. This is the bug being
   fixed — it must be demonstrated, not inferred.
3. Adding a kwarg to a verb, shipping, and letting the watchdog bounce:
   an already-open session can pass the new kwarg without it being
   silently dropped (proves the tool-list refresh).
4. Twelve concurrent sessions each issuing a `search` simultaneously: all
   twelve complete; none blocks on `pool.connection()`. Measure with
   `pool.get_stats()`.
5. `docker ps` shows exactly one `precis-mcp:dev`-family container after
   a day of normal use (today: six, oldest 5 days).
6. An editor save in the main checkout does **not** bounce the server; a
   checkout/ship does.
7. In-flight calls drain: a bounce triggered while a slow `search` runs
   returns that search's result rather than failing it. Must be tested
   against the counter, not the semaphore — the semaphore version of this
   would pass vacuously by never waiting at all.
8. After one bounce, **every** open session's next `precis-status` shows
   why the server restarted — not just whichever session asked first.
9. A second session starting while the container is already up does not
   create a second container, and does not disturb the first.

## Target + blast radius

- `src/precis/install_watchdog.py` — new source-checkout arm, the
  quiesce wait, and the per-session breadcrumb split.
- `src/precis/server.py` — **real logic change, not just verification**:
  the in-flight counter has to be incremented/decremented around
  `_offload_sync`'s `to_thread` call, and the boot path has to read the
  breadcrumb once for later per-session serving.
- `src/precis/handlers/skill.py` — `precis-status`'s breadcrumb read
  becomes session-keyed.
- `src/precis/store/pool.py` — env-resolved pool size defaults.
- `~/work/infrastructure/precis-mcp/scripts/` — new
  `precis-mcp-http-ensure.sh`; `precis-mcp-dev-stdio.sh` **deleted**.
- `.claude/settings.json` — new `SessionStart` hook entry.
- `~/.claude.json` (host-local, not in repo).
- No launchd plist and no Ansible role.

**Blast radius inverts and this is the main cost of the change**: today a
wedged server kills one session, shared it kills twelve. `KeepAlive`
plus criterion 1 is the mitigation. Pagination cursors are also now
shared — better in steady state (a `more(cursor=...)` always finds its
minting process, where today it is per-container) but a restart
invalidates every session's cursors at once.

## Verification state (2026-09-29)

**AC0 PASSED — the premise holds.** The container was stood up by hand and
bounced under a live headless Claude Code session issuing eight sequential
`precis` calls: `ROUNDS=ooxooooo`. Exactly one call failed at the bounce
(`MCP server "precis" session expired`); the client then reconnected on its
own and rounds 4-8 hit the new instance, with no Claude Code restart and no
hard error. The one lost call is the accepted restart window, not a defect.

Server half, measured against the running container rather than the wheel
source: `initialize` issues a 32-char `Mcp-Session-Id` (stateful, as
required); an unauthenticated request gets 401; `tools/list` on a stale id
after `docker restart` gets **404 `Session not found`**.

**Also passing:** AC9 (a second ensure call neither creates a second
container nor restarts the first — same id, same `StartedAt`), AC6 and AC7
in unit form (`tests/test_checkout_watchdog.py`), AC8
(`test_every_session_sees_the_breadcrumb_once`, twelve sessions).

**Live since the gate went green (main @ 2a34fce6, deployed, 2026-09-29
23:14Z).** The shared server was recreated onto the gated code and the
checkout arm is armed for real: `checkout watchdog armed on /src at
2a34fce6c026 (every 5s)`. Sizing confirmed inside the running process, not
just in the wrapper: tool concurrency 12, pool 4/16.

**Dogfooded against prod 2026-09-30 06:40-08:50Z.** Results:

- **AC4 PASSES.** Twelve concurrent sessions, one `search` each, against the
  live shared server: all twelve OK, 4.78 s wall. Single-search baseline is
  2.06 s, so a serialised run would be ~25 s. Nothing queued on the pool.
- **AC1/AC2 bounce half PASSES in production.** A real ship moved main's
  HEAD; the server bounced in 10 s (`drained 0 in-flight call(s)`), rearmed
  on the new sha, and served again. The next client got the per-session
  breadcrumb naming both shas.
- **AC7 failed on the first dogfood; fixed and re-verified.** A bounce during a slow cross-kind search killed it (`drain timed
  out after 20s`, transfer closed, no result after 24.7 s) while a fast
  search in the same setup was delivered in full — two defects, both fixed
  under gr457887. The bound (`_DEFAULT_DRAIN_TIMEOUT_S`) was under real
  search latency: now 120 s and overridable with
  `PRECIS_MCP_DRAIN_TIMEOUT_S`. And `wait_for_drain` waited for the counter
  to reach **zero**, which on a shared server may never happen, so every
  bounce burned the bound and then killed whatever was running: it now
  latches a high-water ticket at bounce and drains only the calls issued at
  or before it, so mid-drain arrivals belong to the next process. The unit
  tests could not catch the second defect because nothing else called during
  them; the two regressions added with the fix keep background traffic
  flowing throughout, and both were confirmed red against the old design.
  **AC7 now PASSES live.** Re-run of the isolated rig against the fixed
  drain (second container on 8766, a throwaway watched checkout,
  `--restart no` — never the live server): HEAD moved 6.4 s into a
  cross-kind `search(kind='*', k=40)`; the search returned HTTP 200 with a
  full 2595-char result after 62.4 s, and the watchdog then logged `drained
  1 in-flight call(s)` and exited 0. The old 20 s bound would have killed
  it three times over.
- **AC2's sha half is BLOCKED — gr457361.** `precis-status` reports the
  *image* build arg (`f2cbcb295ab2`, baked 2026-09-08), not the source being
  served. Pre-existing — an old stdio container reports the same — but it
  makes AC2 unverifiable as written.
- Also found: **gr457326**, md-index vector warmup has no retry, so one slow
  embedder batch at boot leaves the cache cold for the whole process
  lifetime — now shared by every session.

Next: gr457361, then gr457326. AC3 is unblocked.

**Still outstanding — these need a ship and a day of use, not a test:** AC1
in the session (as opposed to headless) client, AC3 (a new verb kwarg surviving a
bounce) and AC5 (one container after a day). AC2's client half is done; its
sha half waits on gr457361. AC4 is done.

**Do NOT sweep the old `precis-mcp-dev-*` containers yet.** Twelve are still
up, and the sessions that started before the config flip are still talking
to them over their stdio pipes — killing one kills that session's MCP. They
age out as those sessions end; only then is AC5 measurable.

**Found while verifying:** gr457326 — the md-index vector warmup has no
retry, so one slow embedder batch at boot leaves the cache cold for the
whole process lifetime. Pre-existing and best-effort by design, but this
item widens its blast radius from one session to all of them.

## Open questions / decisions log

**DECIDED — Reto, 2026-09-29:**

- **Hand-rolled, no Ansible, no launchd.** Lazy start from a
  `SessionStart` hook; first session up creates the container, later ones
  no-op. `deploy/` is fleet config and this is a developer-machine
  service; `docs/backlog/mcps-venv-deploy-gaps.md` is already open on the
  fleet-side `mcps` role and this must not entangle with it. Consequence
  folded into in-scope item 1: without launchd there is no `KeepAlive`,
  so `--restart unless-stopped` has to do that job or the watchdog's
  clean exit leaves every open session dead — strictly worse than today.
- **No fallback.** `precis-mcp-dev-stdio.sh` is deleted, not retained.
- **Restart window accepted.** No retry anywhere.
- **Snapshot via the wrapper, not the image.** The copy lives in the
  `docker run ... sh -c` command, avoiding the image rebuild documented in
  `precis-mcp-dev-stdio.sh`'s header.

**RESOLVED — both `ready` blockers, folded into the spec above:**

- *Quiesce via the tool semaphore is not implementable from a daemon
  thread* → in-scope item 4 now specifies a `threading`-primitive
  in-flight counter instead, and records why the semaphore route is a
  trap (an unawaited coroutine that exits without waiting). AC7 now
  demands the test discriminate between the two.
- *Breadcrumb consume-once regresses to 1-of-12* → new in-scope item 6
  makes it session-keyed on the `serve_ledger` pattern; new AC8 covers it.

**STILL OPEN:**

- **[CLOSED — item 4 rewritten] blocker — quiesce (item 4) is not
  implementable as written; a real
  gap, not a wording nit.** `_get_tool_semaphore()` returns an
  `anyio.Semaphore`; its `.acquire()` is `async def` and is only ever
  `await`ed inside `async with sem:` on the FastMCP event-loop thread
  (`server.py::_offload_sync`, the `wrapper` closure). `InstallWatchdog`
  (`install_watchdog.py:274-316`) is a plain `threading.Thread` with no
  event loop of its own. Calling `sem.acquire()` from that thread does
  not block-and-wait — it returns an unawaited coroutine object (a
  silent no-op, past a `RuntimeWarning` on stderr nobody reads), so
  "acquire all `PRECIS_MCP_TOOL_CONCURRENCY` permits ... with a bounded
  wait" as literally described never runs, and `os._exit(0)` proceeds
  immediately regardless of in-flight calls. Reaching the semaphore from
  a daemon thread needs an `anyio.from_thread` blocking portal set up
  from the async side and handed to the watchdog — a real piece of
  design this item doesn't mention. AC7 cannot be demonstrated against
  the mechanism as specified. This also means "Target + blast radius"
  undersells `server.py`'s footprint: "no logic change expected; verify
  `_get_tool_semaphore` is reachable" is not a verification task, it's a
  missing cross-thread bridge that has to be designed and built.

- **[CLOSED — new item 6 + AC8] blocker — exit-breadcrumb consume-once semantics silently breaks
  under many-sessions-one-process; the spec's own probe area (per-process
  state wrongly shared) has a real hit it missed.**
  `install_watchdog.consume_last_exit_breadcrumb()`
  (`install_watchdog.py:188-217`) deletes the breadcrumb file on first
  read, process-wide, with no session key; `handlers/skill.py`'s
  precis-status handler calls it unconditionally. Item 4 leans on this
  ("Reuse `_write_exit_breadcrumb` so `precis-status` still reports why
  the last server ended") but under one process serving 12 sessions,
  only the *first* of the 12 to call `precis-status` after a bounce sees
  the explanation — the other 11 get nothing, which is exactly the
  "operator found the server simply gone with zero explanation" failure
  (gr341515) this mechanism exists to prevent, now recurring for 11/12
  clients on every bounce. Needs either a per-reader-not-per-file
  breadcrumb (e.g. keep the last N or don't delete on read within some
  window) or an explicit call-out that this is a known, accepted
  regression for the multi-session case.

- **[CLOSED — now AC0, a stop-the-build first-contact check] advisory — AC1-AC3 rest on Claude Code client behavior this repo
  can't verify.** I confirmed the *server*-side half directly against
  the pinned wheel: `mcp==1.28.1`'s
  `StreamableHTTPSessionManager.__init__` defaults `stateless=False`,
  and an unrecognized `Mcp-Session-Id` on a stateful request returns a
  404 `JSONRPCError` ("Unknown or expired session ID - return 404 per
  MCP spec", `streamable_http_manager.py`) — matches the spec's claim.
  Whether Claude Code's own client actually treats that 404 as
  "reconnect, re-`initialize`, refresh the tool list" (vs. surfacing a
  hard error) is external client behavior, not present anywhere in this
  repo and not checkable ahead of time — flagging so AC1 is understood
  as first-contact discovery, not something the build can pre-confirm.

- **[CLOSED — checked live 2026-09-29: both roles rolconnlimit -1] advisory — pgbouncer `agent_rw` connection-limit claim is
  DB-state, not repo-state.** "No role `CONNECTION LIMIT` is set" (sizing
  section) can't be verified by grep/search_code — it's a live
  `pg_roles` setting, only checkable via `scripts/prod-psql`. None of the
  7 acceptance criteria would catch a stale assumption here (e.g. AC4
  measures `pool.get_stats()`, not a role-level cap), so if this is wrong
  the 16-connection sizing could silently starve without tripping any
  listed check.

- **[CLOSED — `model: opus` set] advisory — model tier vs. content.** No explicit `model:` field
  (defaults to sonnet). The quiesce mechanism above needs a genuine
  cross-thread/event-loop design (not present anywhere else in this
  codebase's watchdog code), and the whole item reasons about a new
  failure mode (blast radius inverting from 1 session to 12) — this
  reads as judgment-heavy, architecture-adjacent work per AGENTS.md's
  agent-sizing table. Worth an explicit `model: opus`, or at minimum
  escalating the quiesce sub-piece for separate review.

- **[CLOSED — citation fixed] advisory — minor misattribution, not substantive.** The sizing
  table's "the docstring's own rule" quote ("binding constraint only
  under genuinely pathological fan-out") is `server.py`'s comment on the
  tool-concurrency semaphore (`server.py:89`), not `pool.py`'s
  docstring. Content is accurate, source is just mis-cited.

- **Verified accurate, no action needed:** the `_fingerprint_for`
  site-packages check (`install_watchdog.py:69`) and the dev container's
  `/app:ro` bind-mount + `import precis` from `/app`
  (`precis-mcp-dev-stdio.sh`) both confirm the stated root cause;
  `_run_network_transport`/`_install_token_auth` exist in `server.py`
  and are reached via `precis serve --transport streamable-http`, used
  by `_sandbox_read_mcp.py`; `serve_ledger.py` is genuinely
  session-keyed (`WeakKeyDictionary` + per-call `contextvars.ContextVar`
  bind/unbind in `tools/core.py`) and safe under one-process-many-
  sessions; `pool.py::DEFAULT_POOL_MIN_SIZE`/`DEFAULT_POOL_MAX_SIZE` are
  2/10 with no env override today, matching both the "today" column and
  the "new knobs required" claim; `server.py::_DEFAULT_TOOL_CONCURRENCY`
  is 4; anyio's default to-thread `CapacityLimiter` is 40
  (`anyio/_backends/_asyncio.py`). The `PaginationCache`
  (`_pagination.py`) is thread-safe and shared as designed — cursors are
  high-entropy `uuid4` (or self-describing recipe cursors for `get()`),
  so cross-session collision risk is negligible beyond what the spec
  already discloses (a restart invalidates every session's cursors at
  once).
