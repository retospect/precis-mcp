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
- Compose file: `~/work/infrastructure/compose.yaml` (the shared stack
  definition; not part of this repo).

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

Run commands bare; reach another directory with an **absolute path**
instead of `cd` (`--git-dir=…`, `ls /Users/reto/work/cluster`,
`scripts/prod-psql` with an explicit host var). A log audit found ~60% of
Bash calls carried a redundant `cd` prefix — the single largest source of
wasted tokens across the fleet, which is why this is called out explicitly
rather than left as an assumed default.
