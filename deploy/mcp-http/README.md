# mcp-http — the shared session MCP server and its always-up proxy

`precis-mcp-http-ensure.sh` keeps ONE long-lived streamable-http precis MCP
server running on a dev machine, behind a small Caddy proxy that never
closes the port. Claude Code's SessionStart hook runs it; the first session
creates the containers, every later session no-ops. It is a hand-rolled
dev-machine service (not an Ansible role): the thread is
`docs/backlog/threads/session-mcp-shared-server.md`.

## Who may run what

Agents may run `precis-mcp-http-ensure.sh --status`, a plain run, and
`--recreate` after a deploy (the image rebuild is the release owner's job,
Reto 2026-10-16). `--migrate` and `scripts/mcp-http-install --apply` are
Reto's or the deploy owner's, from a terminal. During an outage run the
INSTALLED ensure script as-is; reconciling the installed copy with the repo
copy is a separate step, not outage handling.

## Layout

```
client ──► 127.0.0.1:8765 ──► <name>-proxy (Caddy, never recreated by an ordinary run)
                                   │  private docker network <name>-net
                                   ▼
                         <name>-a   or   <name>-b      (no host port; one is live)
```

- `<name>` is `precis-mcp-http` unless `PRECIS_MCP_HTTP_NAME` says otherwise.
  The proxy is `<name>-proxy`; `<name>` itself is no container any more.
  That is deliberate: the proxy taking the old name would make
  `docker logs precis-mcp-http` show Caddy's log where everyone expects the
  server's. Now it fails loudly. `--status` prints the live backend's name;
  use `docker logs <name>-a` or `-b`.
- State is under `~/.cache/precis-mcp-http/` (`PRECIS_MCP_HTTP_STATE`):
  `backend` (live colour), `caddy/Caddyfile` (what the proxy routes to; it wins
  if it ever disagrees with `backend`), `ensure.log`, `secrets/`, `cache/`.
- Extra python-kind roots for one host go in `python-roots` there, one
  `name:/absolute/host/path` per line (e.g. a sibling repo). Each is mounted
  read-only at `/roots/<name>` and appended to `PRECIS_PYTHON_ROOTS`; editing
  the file changes the container spec, so the next ensure run swaps in a new
  backend (blue-green). Invalid or missing entries are skipped with a note.
- The installed `precis` CLI on the host is a third copy of the code —
  `precis` serves the image, `main` the checkout, neither is the venv the
  console script runs. The ensure script resolves the console script's
  shebang interpreter (`import precis; dirname(__file__)`), copies the
  package into `cli-python-root/` under the state dir (the discovered path
  can sit outside the dirs the docker VM shares — a bind mount of one
  reads EMPTY inside the container) and mounts the copy read-only at
  `/roots/cli/precis`, so `cli::precis.<mod>.<sym>` answers "what is my
  CLI actually running" (gr520147). The copy refreshes when the source's
  content changes; every discovery outcome is logged, and a backend that
  comes up with no `.py` under any mounted source path is named loudly.
  A `cli` line in `python-roots` wins; `PRECIS_MCP_CLI_PYTHON=<interpreter>`
  overrides discovery, `=off` disables it.
- Git worktrees of the main checkout (mounted at `/main`) appear dynamically
  as `wt-<tree name>` roots in the python and md kinds, discovered from
  `/main/.git/worktrees`
  (`PRECIS_PYTHON_WORKTREES=wt:/main`, host paths translated by
  `PRECIS_PYTHON_GITDIR_MAP`); no restart needed, read-only, lazy; an index is
  dropped after 24 h unused (`PRECIS_PYTHON_WORKTREE_IDLE_HOURS`) or when the
  tree is removed. `PRECIS_PYTHON_WORKTREE_MAX` adds an optional LRU cap
  (default 0 = none). A tree discovered *after* the first listing also gets
  a speculative background index warm, capped at
  `PRECIS_PYTHON_WT_WARM_MAX` warm-built indexes held (default 4; 0
  disables warming).
- One checkout per host serves as the image build context, the `/main` mount
  (and so its worktrees) and the parent of the `-prod` clone that `/src`
  serves. It is `PRECIS_MCP_REPO`, else the absolute path in `main-checkout`
  in the state directory, else `~/work/project/code/precis-mcp`. A host whose
  agents work elsewhere (e.g. `~/precis-mcp`) writes that path to
  `main-checkout`. A resolved path that is not a git checkout is REFUSED
  (exit 3, before any docker call, the running colour untouched) — never a
  fallback to the default. The refusal names where the value came from
  (env, the marker, or the default) so a stale marker is fixable in one
  step. Since gr515239 `main-checkout` is also the canonical-checkout
  marker: a run that finds none records its own resolution (env, then the
  invoker's checkout, then the default), and a later run resolving a
  different physical path refuses before touching docker — a stale second
  copy of the repo once recreated the live backend. `mcp-http-install
  --apply` and `mcp-image-refresh` refuse from a non-canonical tree the same
  way. Backends and the proxy carry a `precis.invoker` label
  (script/pid/ppid/cwd) so the container records who created it, and
  `precis-status` shows the mounted host paths as `src_host`/`main_host`.
- Before deleting a checkout on this host (a strays/second-copy cleanup),
  run `precis-mcp-http-ensure.sh --check-path <path>` — delete ONLY on
  exit 10 ("free"). Exit 0 means a canonical-checkout marker or a
  container mount still references it; any other code means the probe
  could not prove the path free (e.g. docker down) — leave it alone (the
  manual version: `main-checkout`, `--status`, `precis-status`
  `src_host`/`main_host`, or `docker inspect` of the backends).
  `scripts/fleet-cleanup` runs the same probe itself. On 2026-10-25 a
  cleanup removed the marker's checkout while containers still mounted it;
  the pre-guard script then fell back to a nonexistent default and
  crash-looped a standby backend. The guard now refuses instead, but the
  deleted path still leaves every run refusing until the marker is
  corrected.
- The Caddyfile is generated inline by the script (`caddyfile_for`), not a
  template file, because the script is installed as a single file outside the
  repo. The proxy mounts the **directory** `caddy/`, so the atomic `mv` that
  replaces the file is seen inside the container.
- The proxy image is pinned by tag and digest in the script
  (`CADDY_IMAGE_PINNED`): `caddy:2.8.4-alpine@sha256:af32e973...c17`
  (v2.8.4, multi-arch index digest, pulled 2026-10-03). Both machines run the
  same bytes. `docker pull` of that exact reference is the only external image
  this adds.

## What the proxy does

- `GET /proxy-health` is answered by Caddy itself (200 `ok`).
- Everything else goes to the live backend with `lb_try_duration 60s`,
  `lb_try_interval 500ms`, `dial_timeout 2s`: a request that arrives while
  no backend is up waits (measured: a POST issued with the backend stopped
  was answered 200 after the backend restarted 3.5 s later). Only dial
  failures are retried, so a request a backend accepted is never replayed.
- `request_buffers 8MB` is load-bearing: without it Caddy 2.8.4 retries the
  dial but then fails the request 502 `invalid Read on closed Body` (measured
  in the rig). MCP calls are small; a body over the limit streams and cannot
  be retried.
- `flush_interval -1` passes SSE unbuffered. `Host`, `Authorization` and
  `Mcp-Session-Id` reach the backend unchanged.
- A pooled or in-flight connection to a backend that dies mid-request still
  fails that one request (502); it is not replayed on purpose.

## Operations

```
precis-mcp-http-ensure.sh                   # ensure up; quiet unless it acts
precis-mcp-http-ensure.sh --status          # proxy and backend reported separately
precis-mcp-http-ensure.sh --config          # the ~/.claude.json entry (127.0.0.1:<port>);
                                            # bearer = the ${PRECIS_MCP_TOKEN} env ref, never the value
precis-mcp-http-ensure.sh --restart         # respawn the serve child in place
precis-mcp-http-ensure.sh --recreate        # force a blue-green backend swap
precis-mcp-http-ensure.sh --migrate         # one-time cutover (below)
precis-mcp-http-ensure.sh --recreate-proxy  # replace the proxy: a port gap, name a window
```

- **Ordinary run.** Proxy created if missing (never recreated; a different
  pinned spec only prints an advisory), then the live backend is checked:
  spec changed or not running -> blue-green; secrets changed -> rewrite the
  files and SIGHUP the serve child; otherwise nothing.
- **Blue-green recreate.** Start the other colour on the network; wait up to
  `PRECIS_MCP_HTTP_READY_TIMEOUT` (300 s) for an unauthenticated `POST /mcp`
  to it, from inside the proxy, to return the server's own 401; write the new
  Caddyfile to a temp file, `caddy validate` it, `mv` it into place, `caddy
  reload` (graceful); then `docker stop -t 45` the old colour (it drains) and
  remove it. Any failure before the swap leaves the old backend live, removes
  the new one, prints its logs and exits non-zero. A failed validate or reload
  leaves the old Caddyfile and backend live and says so.
- **Log.** Each switch appends `proxy: a→b <UTC timestamp>` to
  `~/.cache/precis-mcp-http/ensure.log`, with `backend:` / `drain:` lines
  around it, so a client's "session expired ... reconnection" can be matched to
  its recreate. Sessions held by the old backend get a 404 on their next call
  and re-initialise; that is the same as before.
- **Health.** `--status` prints `GET /proxy-health` (200 = Caddy is up) and
  `POST /mcp` without a token, directly to the backend from the proxy and
  through the proxy (401 = serving; 502 or no answer = backend down). The
  proxy has no in-process work to wedge; Docker restarts it on exit
  (`--restart unless-stopped`).
- **Upgrading the proxy.** Config changes never need it (`caddy reload`). A
  new pinned image or a new `docker run` spec does: edit `CADDY_IMAGE_PINNED`
  (pull the new reference first), install the script, then run
  `--recreate-proxy` in a window you name. That is the one remaining port gap,
  stop-to-bind of the proxy container (measured 0.3 s on the rig; colima's port
  forwarder can add seconds). The client gives up only after about 31 s.

## Worktree servers (`scripts/wt-mcp`, dev-loop-scratch D1)

A git worktree can run its own copy of this layout — `precis-wt-<tree>-proxy`
on `127.0.0.1:880N` (8801–8809, nine at most), backends `precis-wt-<tree>-a|b`,
state under `~/.cache/precis-mcp-wt/<tree>/` — serving the worktree's code
and skills, read-write on prod with the shared server's role. The same
script runs it, in worktree mode (its header):

```
PRECIS_MCP_HTTP_NAME=precis-wt-<tree> PRECIS_MCP_HTTP_PORT=880N
PRECIS_MCP_HTTP_STATE=~/.cache/precis-mcp-wt/<tree> PRECIS_MCP_SRC=<worktree>
PRECIS_MCP_REPO=<main checkout> PRECIS_MCP_WORKTREE=<tree>
```

`scripts/wt-mcp` (library `scripts/wt_mcp.py`) sets those; nobody types them.

```
scripts/wt-mcp up        start (idempotent); port recorded in .claude/wt-mcp/port
scripts/wt-mcp claude    up + src/ watcher + `claude --mcp-config <state>/mcp.json
                         --strict-mcp-config`: the session's ONLY precis is this server
scripts/wt-mcp watch     the src/ watcher (auto-restart + down-recovery), foreground
scripts/wt-mcp status    probe (proxy 200 / backend 401), watcher pid, --status
scripts/wt-mcp restart   --restart: respawn the serve child, port held
scripts/wt-mcp down      containers, network, uv-cache volume and state removed
```

- **Same name, one server.** The generated config names the server `precis`,
  so tool names stay `mcp__precis__*` and the session has no second precis
  to confuse it (Reto 2026-10-23). The file holds the bearer token: mode
  600, in the tree's state dir under `~/.cache/precis-mcp-wt/<tree>/` (never
  inside the checkout — the public-repo secret gate walks gitignored files
  too), never printed; `wt-mcp` itself never reads the token (`--config`
  output goes straight to the file).
- **What differs in worktree mode.** `PRECIS_MCP_SRC` may be a linked
  worktree (its `.git` is a pointer file); the served tree is never moved to
  `origin/prod`; `PRECIS_SCHEMA_GATE=refuse` with `PRECIS_AUTO_MIGRATE=0`
  pinned (a tree's own migrate path can never beat the gate); the container
  gets `PRECIS_MCP_WORKTREE=<tree>`. With that variable unset the script's
  behaviour and the shared server's container spec hash are unchanged
  (verified: identical `ENV_HASH`/`PROXY_SPEC` on the default path).
- **Which script runs.** `PRECIS_WT_ENSURE`, else the tree's own
  `deploy/mcp-http/precis-mcp-http-ensure.sh`, else the main checkout's. A
  copy without worktree mode (an installed script predating D1) is refused
  rather than silently serving the main checkout.
- **Migration guard, fail closed.** `up`, every watcher `--restart` and
  every recovery ensure compare the tree's migration files (committed,
  staged, untracked) with `origin/prod`; any file prod lacks, or any git
  failure, refuses and names `scripts/dev`. A migration added while the
  server is up stops the auto-restart.
- **Provenance.** The server's instructions open with
  `worktree server <tree> @ <sha>` (the sha read from `/src` through
  `PRECIS_PYTHON_GITDIR_MAP`, so the checkout watchdog also bounces it on a
  commit); every ref it creates carries `meta.worktree`; its `tool_calls`
  rows have `session_id` starting `wt-<tree>-`. It runs no workers: a job it
  mints runs on prod workers and prod code.
- **Auto-restart.** The watcher polls `src/` once a second, waits 2 s of
  quiet after a change, then `--restart`s (child respawn behind the held
  port; the supervisor re-copies `/src` to `/app`). Claude Code's next call
  waits in the listen backlog and re-initialises on the new session id — no
  `/mcp` reconnect. The watcher also brings the server back when the probe
  says down (plain ensure, at most once per 30 s). It only ever names
  `precis-wt-<tree>`: `scripts/wt_mcp.py::assert_wt_target` guards every
  docker and ensure call, and `tests/test_wt_mcp.py` pins it.
- **Seeding.** First `up` clones the shared server's `cache/` (the md vector
  cache, APFS clone) and `python-roots` into the tree's state dir so the
  worktree server does not re-embed from cold. Secrets are regenerated by
  the script, never copied.
- **Cleanup.** `scripts/reap-worktrees` runs its own checkout's
  `scripts/wt-mcp down --root <tree> --if-present` (bounded by a timeout)
  before removing a tree; `down` re-lists the family and exits non-zero if a
  container survived, and the reaper then keeps the tree rather than orphan
  a prod read-write server. One tree root per server name: a second
  worktree with the same directory name is refused at `up`.
- **Attach an already-running session.** `up` writes the client config to
  the tree's ensure state dir: `~/.cache/precis-mcp-wt/<tree>/mcp.json`
  (`$PRECIS_WT_STATE_HOME/<tree>/mcp.json` when set; `wt-mcp status` prints
  the path). MCP config is launch-time only — a session started without it
  picks the tree server up by relaunching:

  ```
  claude --mcp-config ~/.cache/precis-mcp-wt/<tree>/mcp.json --strict-mcp-config
  ```

  the same argv `wt-mcp claude` execs, so the tree server is again the
  session's only `precis`. `up` alone starts no watcher: `wt-mcp claude`
  runs one for its session's lifetime; without a Claude session,
  `scripts/wt-mcp watch` is the foreground form.

## One-time cutover from the single-container layout

The only gap in this design. Today's single `precis-mcp-http` container
publishes the port itself; the proxy cannot take the port until it has gone.
After copying the new script over the live one, an ordinary run **leaves the
old container serving** and prints a notice (it does not migrate by itself from
a SessionStart hook). In a window you name, run:

```
precis-mcp-http-ensure.sh --migrate     # --recreate does the same on the old layout
```

Order: create the network; start backend `-a` and wait for its 401; create the
proxy (not started); stop the old container (it drains, the port stays open and
queues connections while it does); remove it; start the proxy. The dark window
is drain-end to proxy start (0.3 s on the rig, plus colima's forwarder). Tell the
orchestrator the minute before, so the fleet reconnect check runs after. If
`-a` never becomes ready, the old container is untouched.

The auto-mode classifier denies a session writing the live script: Reto copies
the file.

`scripts/mcp-http-install` makes that drift visible: with no arguments it
compares sha256 of the installed script (`PRECIS_MCP_ENSURE`, default the
path the SessionStart hook runs) with this repo copy and prints one line, in
sync or differs; the SessionStart hook runs it right after the ensure script.
`--apply` shows the diff, backs the installed file up to
`<installed>.bak-<UTC stamp>` and copies the repo file over it (mode 755). It
never runs the ensure script: run `--recreate` (or `--migrate` if `--status`
says LEGACY) yourself afterwards.

## Install / prerequisites

- Image `precis-mcp:dev` built as in the script's error text (unchanged).
- `~/.secrets/pw/PRECIS_MCP_DB_HOST` holds the database node's Tailscale
  hostname (one line). This file is public and cannot name the node; the old
  script had it as a literal. `PRECIS_MCP_DB_HOST` in the environment also
  works.
- Client side (gr515184): the bearer token lives in `~/.config/precis/token`
  (mode 600, ansible-managed; `~/.secrets/pw/PRECIS_MCP_TOKEN` is the same
  value and serves until that file lands). `--config` emits the
  `${PRECIS_MCP_TOKEN}` env reference — never the value — so
  `claude mcp get precis` can't leak it; `scripts/mcp-token-env` fills the
  variable into a session's environment at launch, and
  `scripts/mcp-token-check` asserts both files' owner/mode plus one
  authenticated call. The mounted `secrets/PRECIS_MCP_TOKEN` must be owned
  by the uid the container runs as or the 0600 mode locks the server out.
- Docker's default address pools hold about thirty networks. A machine with
  many compose test stacks can exhaust them ("all predefined address pools have
  been fully subnetted", seen with 30 present); the script then retries with an
  explicit subnet (`10.213.77.0/24`, or `PRECIS_MCP_NET_SUBNET`).
- Both backend colours mount the same secrets directory, md vector cache and
  uv cache volume; during a swap the two overlap for the new one's start-up,
  so the DB pool (`PRECIS_DB_POOL_MAX_SIZE`, 16) briefly counts twice.
- The kind-surface env (`PRECIS_CHEM_ENABLED`/`PRECIS_CHEM_ROUTE_NODE`,
  `PRECIS_BIO_ENABLED`/`PRECIS_FOLD_NODE`, `PRECIS_AUTOCATPATH_ROUTE_NODE`,
  `PRECIS_DFT_NODE`) is rendered from `precis_capabilities` in the gitignored
  overlay by `scripts/lib/cluster-topology.sh`, mirroring the precis-web
  plist — never a committed node literal. A missing overlay leaves the kinds
  dark; it never fails the ensure (gr489468).

## The second machine

The same script. Its three deviations are unchanged: `PRECIS_MCP_REPO` (a
different clone path), the database password folded into the secret file, and
the image built with a GitHub token. `follow_prod` moves the served clone at
SessionStart; the backend's checkout watchdog then respawns the serve child,
which the proxy never notices. The proxy reads only its Caddyfile in the state
directory, which a deploy run from another machine cannot touch.

## Test rig

`tests/test_mcp_http_proxy_rig.py` drives this script on its own name, port,
state, HOME and docker network against a fake backend. Skipped unless docker is
reachable and `PRECIS_MCP_PROXY_RIG=1`:

```
PRECIS_MCP_PROXY_RIG=1 uv run pytest tests/test_mcp_http_proxy_rig.py -n0 -s
```

The script's rig hooks (`PRECIS_MCP_RUN_CMD`, `PRECIS_MCP_ENTRYPOINT`,
`PRECIS_MCP_CADDY_EXTRA`, `PRECIS_MCP_DB_IP`, `PRECIS_MCP_CADDY_IMAGE`, plus the
older port/state/image/src overrides) are refused on the live name unless
`PRECIS_MCP_LIVE=1` (gr462596).
