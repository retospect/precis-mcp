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

- On a **successful deploy**, `scripts/deploy` writes a gitignored
  `.deploy-state` marker (`<sha> <epoch>`) at the repo root recording what's
  actually running on the cluster.
- At the **start** of a ship, if the oldest undeployed commit is older than
  `PRECIS_DEPLOY_STALE_HOURS` (default `1`), `scripts/ship` prints a loud
  `⚠ deploy lag` warning — the "begin of next ship burst" moment is the
  cheapest place to notice drift.
- At the **end** of a successful ship, it prints a one-line `📦 N commit(s)
  … not yet deployed (oldest Xh ago)` summary (skipped silently if
  `.deploy-state` is absent, e.g. never deployed from this worktree).

All of this is best-effort git plumbing guarded with `|| true` — it can
never fail or block a ship. A future `PRECIS_AUTODEPLOY_STALE=1` could opt
into *actually* invoking `scripts/deploy` past the stale threshold, but
that isn't implemented — this change only surfaces the lag.
