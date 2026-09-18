# Verifying a few named test files when the local gate queue is hours deep

**When.** `scripts/test` waits on a 2-slot local gate (`scripts/lib/gate-slot.sh`)
and the queue is deep (seen 2026-09-16: ~20 sibling waiters, some over 1.5 h).
You need a handful of named test files green before a `/qland`, not the full
suite. The remote `/land` matrix or the post-burst `/go` stays the real gate;
this is a spot check.

**Do not** dispatch a `test-runner` agent into a deep queue (it cannot wait
usefully and flails on empty logs), and do not kill or stop gate containers
(they belong to sibling sessions). Your own parked `bash scripts/test …`
waiter holds no container, so a plain `kill <pid>` on it is safe.

## Recipe

1. Start the test DB for this worktree's compose project (once per session):

   ```
   docker compose -f docker/dev/compose.yaml -p "$(compose_project_for "$PWD")" --profile dev \
     up -d --wait precis-test-db
   ```

   `compose_project_for` comes from `scripts/lib/compose-project.sh`. Source
   it from a bash script, not the interactive zsh session (the sourced
   function's `cd` loses `tr` under the harness zsh).

2. Run the named files at `-n0` with an explicit test-DB URL:

   ```
   docker compose -f docker/dev/compose.yaml -p "$(compose_project_for "$PWD")" --profile dev \
     run --rm --no-deps -v "$PWD":/app -e UV_LINK_MODE=copy \
     -e PRECIS_TEST_PG_URL=postgresql://postgres@precis-test-db:5432/precis_test \
     precis-dev bash -lc 'uv run pytest -n0 -q -rs -p no:cacheprovider tests/<files>'
   ```

3. **Check the skip count.** `scripts/dev` alone carries no test-DB URL, so
   every `db`-tagged test silently skips and the run looks green (96 skips
   once passed for a pass). `-rs` lists them; a db-tagged file with skips
   means the URL did not reach the container.

## Why the bypass is safe

The slot guard is an out-of-memory guard for `-n6` full-suite gates. A
`-n0` run of a few files is light and does not compete for the memory the
guard protects. It is still not a ship gate: `/land`'s remote matrix, or
`/go`'s full local suite over the integrated main, is.

## Starvation, not only depth (2026-09-17)

The slot grab polls every 3 s with no queue, so a `scripts/ship --remote`
hybrid re-gate waited 80+ min while slot 0 cycled through four siblings, and
the ship lock likewise passed through three sibling ships ahead of it (six
`/land` attempts, three green CI runs, zero merges, because main moved every
time). Under a burst, `/qland` each tree, then one `/go`, is the only path
that lands. Tracked as gr343941.
