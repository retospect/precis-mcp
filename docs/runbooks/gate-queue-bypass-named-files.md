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

## First: find out who actually holds the two slots

A slot is a **holder file**, not a container. `docker ps` reads backwards
here and has misled three sessions into diagnosing a deadlock (2026-09-28).
The authoritative read:

```
cat /Users/reto/precis-mcp/.git/precis-gate-slot-{0,1}.lock.d/holder
  -> <worktree path> pid=<pid> host=<host>
ps -o etime=,command= -p <pid>
```

- `precis-test-<tree>-precis-gate-1` on `tini -- sleep infinity` at ~0.7%
  CPU is a **warm reusable container** in its normal resting state. It
  holds **no** slot; killing it frees nothing and costs a rebuild.
- The containers doing work are `precis-test-<tree>-precis-dev-run-*`
  (~22% CPU, ~2.3 GiB) plus `precis-test-<tree>-precis-test-db-1` (100%+
  CPU, ~10 GiB).
- **`ps` elapsed far exceeding container age is queue wait, not a hang** —
  most of a holder's elapsed time was spent waiting for the slot it now
  holds.

Never raise `PRECIS_GATE_SLOTS` to get unblocked: `scripts/lib/gate-slot.sh`
records that a third gate into a 2-slot semaphore caused the OOM churn the
guard exists to stop (gr202193). Wait, use the bypass below, or ask the
holder's session.

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
