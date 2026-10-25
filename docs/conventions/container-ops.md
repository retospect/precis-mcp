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

### Docker VM prune (standing rule, Reto 2026-10-16/17)

The coordinator may prune the Colima VM when free space is under 30 GB
(`docker run --rm alpine df -h /`; the gate's `docker_free_gb` probe can
misread) or a ship fails with "No space left on device", in order, stopping
once over 30 GB:

1. `docker builder prune -f --filter until=24h`
2. `docker image prune -f` (dangling only)
3. `docker builder prune -f`, then `docker rmi <tag>` on one-off
   `*-verify` / `*test*` / `*-ci-*` images no container uses
   (`docker ps -a --format '{{.Image}}'`).

Never rmi `precis-dev:latest`, `precis-mcp:dev`, `precis-mcp:premodels` or
an image a container uses. Never `-a`, `docker volume prune`,
`docker system prune`, or stop/remove containers. Growing the disk
(`colima stop && colima start --disk N`) stops every container: Reto's call.
Keep concurrent local `--checked` ships to 3 or fewer.

### Scripted `psql` over ssh needs `-X`

`psql` sources the remote user's `~/.psqlrc` unless given `-X`; on the data
host `\timing` writes `Time: …` to stdout and `\pset null` rewrites NULLs, so
`-tAc "SELECT count(*)"` returns `0\nTime: 0.4 ms`. Anything that ssh's to a
host and parses psql stdout passes `-X` (`scripts/prod-psql` scripted
branches, `scripts/gen-schema`, ansible `shell:` psql tasks); pinned by
`tests/test_scripted_psql_skips_psqlrc.py`. When a comparison never
matches, look at the bytes (`| od -c`) before theorising.

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
non-git ops an **absolute path** is fine (`ls /Users/reto/precis-mcp`, <!-- home-path: allow — example of a fine absolute path -->
`scripts/prod-psql` with an explicit host var).

Do not route around that refusal with a wrapper script that shells out
internally — the guard matches the command text, so the indirection "works",
but it defeats a boundary the harness enforces deliberately. If
`scripts/inflight --json` lacks a field you need, add it there.

Note the worktree-isolation guard is text-based, so it also fires on
commands that merely *mention* the tool in a heredoc or a quoted string.
When that blocks a legitimate edit, use the Edit/Write file tools instead
of a shell script.

"Worktree guard" names three different mechanisms — grep for the one you
mean:

- **the harness's worktree-isolation guard** — built into the driver, no
  repo file: refuses compound Bash shapes and `git -C`/`cd` redirects out
  of this tree.
- `scripts/hooks/guard-cd-to-primary.py` — PreToolUse Bash hook that
  denies a segment running after `cd` into the primary checkout.
- `scripts/hooks/guard-worktree-path.py` — PreToolUse Edit/Write hook that
  rewrites an absolute path pointing at the main checkout to its worktree
  twin.

### Refused shapes and the accepted alternative

5-day audit (gr456287): 308 refusals across 97 of 99 sessions. The
worktree-isolation guard fails closed whenever it cannot prove from the
command text that git stays
in this worktree — anything it cannot fully parse statically is refused,
so write the plain shape first rather than after the refusal.

The accepted shape is **one plain Bash command per call**: a single literal
command line — no loops, no `$(...)`, no `&&`/`;`/`||` chains, no heredocs,
no pipes around `git`, no variable assignments. A command that genuinely
needs one of those goes in a file: write the script with the Write tool,
then run it as one plain command (this does not launder a refused
operation — see the routing-around paragraph above). Independent commands
go out as parallel Bash calls. Enforcement varies by driver — the guard
is a backstop, not the boundary, so a chain another driver tolerates is
still off-shape (gr514492).

The guard's contract is narrower than the rule: it refuses only what it
cannot statically prove keeps git in this worktree, so
`git add … && git status` and a git-free `python3 - <<EOF` heredoc both
RUN (gr514330, gr514423). The one-command shape stays the
fleet rule because anything the guard cannot fully parse is refused —
the plain shape is the one that never surprises.

| Refused | Accepted |
|---|---|
| `git` inside a loop, pipeline, subshell, `$(...)` or `&&`/`;` chain with variable assignments (68 "git in a construct") | One plain `git …` per Bash call; independent commands as parallel calls |
| `git -C <other tree> …` (18) | `scripts/inflight --json` for other trees; bare `git …` for this one |
| `rtk git …`, i.e. the rtk hook's rewrite of `git …` (34) | `RTK_DISABLED=1 git …` or `/usr/bin/git …`; see `rtk.md` Known gaps |
| `X=1; python3 -c "print($X)"` — an interpreter given a dynamically computed program, even with no git in it | A static program: a literal `-c '…'` with no `$` expansion, or a script file written with the Write tool and then run |
| `python3 - <<EOF` / shell heredoc whose text names git | Edit/Write file tools |
| `git commit -m "$(cat <<'EOF' …)"`, or `-m "…"` quoting `'`/`` ` ``/`$` | `git commit -F scratch/commit-msg.txt` — write the message with the Write tool; `scratch/` is the named gitignored scratch dir (gr514617, gr514743, gr515326, gr515823) |
| `mkdir -p /tmp/x && LOG=… && scripts/ship … > "$LOG" 2>&1` ("construct too complex", 95) | Split into plain commands; a literal `> /tmp/x.log 2>&1` redirect |
| `LOG=/tmp/x.log` or any variable holding a path, referenced as `"$LOG"` (even alone) | Literal paths — the guard cannot trace a variable's value |
| `grep`/`find` whose arguments contain the token `git` | Grep/Read tools |

`github`, `git_sha` and similar word-boundary near-misses pass. The
worktree-isolation guard is a harness built-in; the upstream asks are
tracked in
`docs/backlog/worktree-path-guard-false-positives.md`.

Package availability / licence checks are one plain command each — the
refusal is the loop/`cd` wrapper, never the check (gr514219):
`scripts/dev uv pip install --dry-run <pkg>` resolves the candidate
inside the container (the `Would install` line is the availability
answer), and licence metadata comes from the PyPI JSON API
(`curl -s https://pypi.org/pypi/<pkg>/json`), one call per package.

A log audit found ~60% of Bash calls carried a redundant
`cd` prefix — the single largest source of wasted tokens across the fleet,
which is why this is called out explicitly rather than left as an assumed
default.

## Fresh worktrees seed `.venv`, never build one (gr514738)

The first `uv run` in a fresh worktree used to resolve and install ~160
packages mid-task. New worktrees instead get a **copy of the main
checkout's `.venv`**, planted by `scripts/seed-worktree-venv` and wired
into `scripts/devin-tree` and `scripts/fleet-wave::_ensure_tree` (the
`claude -w` creation path lives in .claude hooks; a tree made by hand can
run `scripts/seed-worktree-venv <tree>` itself). `cp -Rc` makes the copy
an APFS clone — instant, no extra disk; `cp -a` elsewhere.

A copied venv is not automatically right: console-script shebangs
(`bin/pytest`, `bin/precis`) and the `VIRTUAL_ENV=` lines in
`bin/activate*` carry absolute paths — possibly a THIRD checkout's, since
main's own .venv may itself be a seed. The seed rewrites every
`*/.venv` path in `bin/*` text files to the new tree, repoints the
editable `.pth`/`direct_url.json` entries at the new `src/`, and stages
the copy under `.venv-seed-<pid>` so a failed seed never leaves a half
venv uv would try to reuse. uv self-heals the rest on the next sync.

## bash 3.2 gotchas (stock macOS bash)

Repo scripts must run on the bash 3.2 macOS ships (`scripts/test` header
says so outright). Two expansions that are fine on bash ≥4.4 but break or
corrupt data on 3.2 — both seen in the wild (gr515242, gr515243):

- **`"${arr[@]}"` on an EMPTY array under `set -u`** → `unbound variable`;
  the loop/exec dies before doing anything. `"${arr[*]}"` dies the same
  way. Guard with the idiom already used across `scripts/deploy`,
  `scripts/fleet`, `scripts/inflight`:
  `${arr[@]+"${arr[@]}"}`. Safe as-is: `"${arr[@]:0:n}"` slices,
  `"${arr[@]:-x}"`, `${#arr[@]}` counts, and any expansion inside a
  `${#arr[@]}` non-empty check.
- **`IFS=$'\t' read -r a b c` collapses empty fields** — tab is IFS
  whitespace, so a run of tabs counts as one delimiter and a producer's
  empty middle column (`git for-each-ref`'s `%(worktreepath)` for a
  branch with no linked worktree; an awk field that came out unset)
  shifts every later field left. Fix: emit a non-whitespace delimiter —
  `\x1f` in bash, `\037` octal in awk printf (`scripts/inflight`,
  `scripts/memory-lint` both do this) — or split with parameter
  expansion. `IFS='|'`/`IFS=','`/`IFS=$'\x1f'` need no fix; only IFS
  *whitespace* collapses.

Pinned by `tests/test_bash32_gotchas.py` — a ratchet on new bare
`"${arr[@]}"` expansions of `()`-initialised arrays and new multi-var
`IFS=$'\t' read` sites; adding either is a deliberate act that updates
the test's audited-sites table.

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
  --brief FILE --tier R|W` (aliases `readonly|qland`; a `claude -w NAME`
  window, or `--devin` via `scripts/devin-tree`; the tree's permission
  files come from `scripts/fleet-authority apply`, the footer from
  `.claude/fleet/generated/footer-<tier>.txt` plus
  `.claude/fleet/builder-workflow.txt` with the coordinator and release
  owner filled from `FLEET_*` env or flags), `peek WIN…`, `ctx`
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
