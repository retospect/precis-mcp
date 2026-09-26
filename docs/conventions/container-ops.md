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
- Compose file: `docker/dev/compose.yaml` (checked into this repo; the
  dev-loop scripts default to it, resolved relative to their own location —
  override with `$PRECIS_COMPOSE` to point at a different/shared stack).

The reason this is a hard rule rather than a preference: a bare local
`pytest`/`psql`/`python` invocation on the host either targets the wrong
database (there is no local `precis_test` outside the container network)
or a Python without the baked extras (torch-free host — see
`docs/conventions/testing.md`). Going through the wrapper is what makes an
op reproducible across machines and across sessions.

## Never `cd` into your own worktree

The Bash shell already runs in the worktree root, and the harness
re-anchors cwd there after **every** call — so a `cd <worktree> && …`
prefix is pure redundancy on every single command. It also risks tripping
the "`cd` in a compound command can trigger a permission prompt" footgun
for no benefit.

Run commands bare; reach another tree with `git -C <path> …` (the mandated
way to read the primary checkout or a sibling worktree — a `cd` into the
primary tree, siblings included, is hard-blocked by `guard-cd-to-primary.py`;
a `cd` to an unrelated repo is not) or an **absolute path** for
non-git ops (`ls /Users/reto/precis-mcp`, `scripts/prod-psql` with an
explicit host var). A log audit found ~60% of Bash calls carried a redundant
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
