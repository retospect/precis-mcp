# Convention — container-first dev ops, and working directory discipline

## Container-first

Dev tooling always goes through a wrapper script that targets the
container stack, never a bare local binary pointed at ad-hoc state:

- `scripts/dev` → a dev shell inside the container image (bakes all
  extras — torch, marker, sentence_transformers, …).
- `scripts/db` → `psql` against the **LOCAL** `precis` / `precis_test`
  databases only (dev pgvector container at `127.0.0.1:5432`,
  `POSTGRES_USER=postgres`). It does not reach prod — see
  `scripts/prod-psql` for that.
- `scripts/prod-psql` → the live prod DB through pgbouncer, as `agent_rw`;
  `--ro` connects as `agent_ro` inside `BEGIN READ ONLY`, the guard to use
  for reads. A targeted write (one INSERT, or one UPDATE/DELETE with a
  WHERE) passes the permission hook; prod-psql runs SQL mentioning
  UPDATE/DELETE in a transaction and rolls it back (exit 4) when it changed
  more than `PRECIS_PROD_WRITE_MAX_ROWS` rows (default 100). **Never a session-level `SET`/`RESET` through the prod DSN**:
  pgbouncer pools by transaction, so the setting sticks to a shared server
  connection and every later client inherits it — one
  `SET default_transaction_read_only = on` "read-only guard" broke every
  agent_rw write fleet-wide on 2026-10-02. The script refuses those
  statements; `SET LOCAL` inside your own transaction is fine.
  `scripts/guide-web`'s header comment records the same trap from 2026-09-04
  and why it uses `agent_ro`.
- Compose file: `docker/dev/compose.yaml` (checked into this repo; the
  dev-loop scripts default to it, resolved relative to their own location —
  override with `$PRECIS_COMPOSE` to point at a different/shared stack).

The reason this is a hard rule rather than a preference: a bare local
`pytest`/`psql`/`python` invocation on the host either targets the wrong
database (there is no local `precis_test` outside the container network)
or a Python without the baked extras (torch-free host — see
`docs/conventions/testing.md`). Going through the wrapper is what makes an
op reproducible across machines and across sessions.

### One compose project per worktree needs a wide Docker address pool

Every worktree's `scripts/test`/`scripts/ship` brings up its own compose
project (`precis-test-<tree>_default` network, see
`scripts/lib/compose-project.sh`), so a 20–40-tree burst holds that many
networks at once. OrbStack's stock engine config hands Docker only 30
scattered `192.168.x.0/24` pools; past that every new gate dies with
`all predefined address pools have been fully subnetted` and
`scripts/reap-test-dbs` cannot help because the networks belong to live
trees. Fix once per machine in `~/.orbstack/config/docker.json` (OrbStack's
`daemon.json`), then `orb restart docker` — the restart stops every
container, so do it between bursts:

```json
"default-address-pools": [{ "base": "172.20.0.0/14", "size": 24 }]
```

That is 1024 networks. Pick a `10.x/16` base instead if your LAN uses
172.20–172.23. Existing networks keep their old subnets; only new ones draw
from the pool. Applied on Reto's Mac 2026-10-10.

## Never `cd` into your own worktree

The Bash shell already runs in the worktree root, and the harness
re-anchors cwd there after **every** call — so a `cd <worktree> && …`
prefix is pure redundancy on every single command. It also risks tripping
the "`cd` in a compound command can trigger a permission prompt" footgun
for no benefit.

Run commands bare. **Another tree is not reachable by git at all** from a
worktree-isolated session: a `cd` into the primary tree or a sibling is
hard-blocked by `guard-cd-to-primary.py`, and a `-C` redirect at another tree
is refused by the harness itself ("a worktree-isolated session's operations
must target its own worktree"). That refusal is **not** a repo hook and cannot
be tuned here — `guard-worktree-path.py` only auto-corrects Edit/Write paths.
To read another worktree's state, use `scripts/inflight --json`: it derives
per-tree dirty counts, ahead/behind, and a capped diffstat at call time. For
non-git ops an **absolute path** is fine (`ls /Users/reto/precis-mcp`,
`scripts/prod-psql` with an explicit host var).

Do not route around that refusal with a wrapper script that shells out
internally — the guard matches the command text, so the indirection "works",
but it defeats a boundary the harness enforces deliberately. If
`scripts/inflight --json` lacks a field you need, add it there.

Note the guard is text-based, so it also fires on commands that merely
*mention* the tool in a heredoc or a quoted string. When that blocks a
legitimate edit, use the Edit/Write file tools instead of a shell script.

### Refused shapes and the accepted alternative

5-day audit (gr456287): 308 refusals across 97 of 99 sessions. The guard
fails closed whenever it cannot prove from the command text that git stays
in this worktree. Write the plain shape:

| Refused | Accepted |
|---|---|
| `git` inside a loop, pipeline, subshell, `$(...)` or `&&`/`;` chain with variable assignments (68 "git in a construct") | One plain `git …` per Bash call; independent commands as parallel calls |
| `git -C <other tree> …` (18) | `scripts/inflight --json` for other trees; bare `git …` for this one |
| `rtk git …`, i.e. the rtk hook's rewrite of `git …` (34) | `RTK_DISABLED=1 git …` or `/usr/bin/git …`; see `rtk.md` Known gaps |
| `X=1; python3 -c "print($X)"` — an interpreter given a dynamically computed program, even with no git in it | A static program: a literal `-c '…'` with no `$` expansion, or a script file written with the Write tool and then run |
| `python3 - <<EOF` / shell heredoc whose text names git | Edit/Write file tools |
| `mkdir -p /tmp/x && LOG=… && scripts/ship … > "$LOG" 2>&1` ("construct too complex", 95) | Split into plain commands; a literal `> /tmp/x.log 2>&1` redirect |
| `grep`/`find` whose arguments contain the token `git` | Grep/Read tools |

`github`, `git_sha` and similar word-boundary near-misses pass. The guard
is a harness built-in; the upstream asks are tracked in
`docs/backlog/worktree-path-guard-false-positives.md`.

A log audit found ~60% of Bash calls carried a redundant
`cd` prefix — the single largest source of wasted tokens across the fleet,
which is why this is called out explicitly rather than left as an assumed
default.

## Ship vs deploy — surfacing lag, never auto-deploying

`scripts/ship` (commit → main) and `scripts/deploy` (main → cluster) are
deliberately decoupled — deploy is a heavy outward action (bounces every
daemon fleet-wide), so it stays an explicit, opt-in step (`scripts/deploy`
or `/go`), never an automatic side effect of a ship. But main can silently
accumulate shipped-but-undeployed commits, so `scripts/ship` **surfaces**
that gap (never blocks on it):

- On a **successful deploy**, `scripts/deploy` writes a shared marker
  (`<sha> <epoch> <outcome>` in the git common dir, `.git/precis-deploy-state`
  — visible to every worktree; the `<outcome>` field is additive, gr338201 —
  old two-field markers still read fine) recording what's actually running on
  the cluster. At deploy start it writes an *attempt* stamp
  (`precis-deploy-attempt`), removed on success — a surviving stamp means the
  last deploy never went green, unless its outcome is `refused` (see below),
  in which case no host was ever touched.
- **Rollback guard** (gr338201): before touching anything, `scripts/deploy`
  refuses when the resolved target sha is a strict ancestor of either the
  deployed-state marker's sha or a freshly-fetched `origin/main` — the real
  incident this fixes was a stale worktree pinned to an old sha rolling the
  whole fleet backward. Equal-sha (no-op redeploy) and a missing marker
  (first-ever deploy) both proceed; `--force-rollback` is the sole override.
- **Backup-window guard**: inside the nightly backup window (03:00–04:20
  UTC; bounds next to the pg_dump cron in
  `deploy/roles/backups/defaults/main.yml`) `scripts/deploy` refuses a
  deploy that adds a migration relative to `origin/prod` — the dump holds
  ACCESS SHARE on every table and the migration's DDL would hang behind it —
  and warns-and-proceeds for a code-only deploy. `--ignore-backup-window`
  overrides. Logic in `scripts/lib/backup-window.sh`; runbook
  `docs/runbooks/migration-deploy-window.md`.
- **Wheel smoke** (gr451360): before touching any host, `scripts/deploy`
  also runs `scripts/wheel-smoke`, which builds the wheel, installs it into
  a scratch venv, and imports `precis_web.app` with no repo `src/` on
  `sys.path` — the artifact-level check that would have caught the
  2026-09-26 outage (a package missing from
  `[tool.hatch.build.targets.wheel] packages`, invisible to every
  worktree-run test) before it went out; `PRECIS_DEPLOY_SKIP_WHEEL_SMOKE=1`
  bypasses it.
- At the **start** of a ship, if the oldest undeployed commit is older than
  `PRECIS_DEPLOY_STALE_HOURS` (default `1`), `scripts/ship` prints a loud
  `⚠ deploy lag` warning — the "begin of next ship burst" moment is the
  cheapest place to notice drift.
- At the **end** of a successful ship, it prints one honest line (gr332009):
  the `📦 N commit(s) … not yet deployed` count when a success marker exists;
  `deploy state uncertain` when the last attempt never recorded success; or
  `no successful deploy on record` when no marker exists. It never counts
  from a stale per-worktree file (the legacy fallback fabricated lag).

All of this is best-effort git plumbing guarded with `|| true` — it can
never fail or block a ship. A future `PRECIS_AUTODEPLOY_STALE=1` could opt
into *actually* invoking `scripts/deploy` past the stale threshold, but
that isn't implemented — this change only surfaces the lag.

## Coordinator tooling: builder waves and cluster liveness

- **`scripts/fleet-wave`** drives a builder wave in tmux: `launch NAME
  --brief FILE --tier readonly|qland` (a `claude -w NAME` window, or
  `--devin` via `scripts/devin-tree`; the footer comes from
  `.claude/fleet/footers/<tier>.txt` with the coordinator, release owner and
  approval quote filled from `FLEET_*` env or flags), `peek WIN…`, `ctx`
  (context % per Claude pane, from the status line), `prompts` (panes at a
  dialog, with the command), `send-line WIN TEXT` (refuses a pane with a
  dialog open or unsent text in its input box), `watch-done NAME…` and
  `stuck-watch [NAME…]` (Monitor-friendly, one line per transition), and
  `close NAME…` (kill the window, `git worktree remove` without `--force`,
  delete the branch only after a clean removal, log it). It calls tmux with
  argument lists only. Session, worktree root and model come from
  `FLEET_TMUX_SESSION`, `FLEET_WORKTREES`, `FLEET_MODEL`. The runbook is the
  `gripe-fleet` skill. The thread-session fleet is `scripts/fleet`, a
  separate tool.
- **`scripts/cluster-ping [NODE…]`** prints one line per inventory host
  (ping, then `ssh uptime` bounded by ConnectTimeout/ServerAlive options, so a
  wedged host fails in about 15 s) and exits 1 if any host fails ssh. Hosts
  come from the gitignored overlay through `scripts/lib/pgb-host.sh`.
