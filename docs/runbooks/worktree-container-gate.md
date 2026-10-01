# Gating a worktree in-container: `scripts/test` and its traps

**When.** Running the suite for worktree edits, or a db test fails in a way
that looks unrelated to your change.

Gating worktree edits in-container is just `scripts/test` **run from the
worktree** (it mounts the cwd at `/app`). Running it after a `cd` to the main
repo silently gates MAIN — a false green. The old manual
`docker compose … -v <worktree>:/app` recipe is superseded. Gate-slot queue:
[`gate-queue-bypass-named-files`](./gate-queue-bypass-named-files.md); image
rebuilds: [`dev-image-rebuild`](./dev-image-rebuild.md); a wedged test DB:
[`reset-test-db`](./reset-test-db.md).

## Traps that survive the script

- **Full-hub MCP path hits prod.** `server.mcp.call_tool` →
  `precis.tools.core._get_runtime()` lazily calls `build_runtime()`, which
  connects to the *configured* DB (prod `agent_rw` in the dev container), **not**
  the test store; setting `server._runtime=` does not redirect it. A test that
  exercises that path must also set `precis.tools.core._runtime`. (One shared
  `precis_test` DB, no isolation.)
- **`DeadlockDetected` pytest errors** are pre-existing shared-`precis_test`
  concurrent-session flakes (see the conftest `_initialise_test_db` docstring),
  not regressions; the failing set varies run to run.
- **Ownership trap.** Running HOST pytest as the `postgres` SUPERUSER against
  `precis_test` leaves migrated tables postgres-owned; the gate's low-privilege
  `precis` role then hits `InsufficientPrivilege: permission denied for table
  _migrations` on every db test. Fix: re-own all public objects to `precis` — a
  DO loop of `ALTER … OWNER TO precis` over `pg_tables` / `pg_sequences` /
  `pg_views`, run as postgres via `docker exec -i` (`REASSIGN OWNED BY
  postgres` fails — system role). Don't switch the gate to the superuser to
  paper over it.
- **Stale baked venv vs a sibling's new CORE dependency.** A
  `ModuleNotFoundError` on a dep that **is** in `pyproject.toml` (hit: `shapely`,
  added by the pcb tiling slice — broke 8 pcb modules and ~20
  `test_draft_handler` cases, which looks like your own regression and is not).
  The image bakes the venv and runs `--no-sync`, so a dep landed on `main` after
  the last bake is simply absent. **Neither `--rebuild` nor `--rebuild-base`
  refreshes it.** Escape hatch: `UV_WITH="--with <dep>" scripts/test …` (the
  ship gate itself syncs from `uv.lock`, so this only affects the fast loop).
- **Connection-pool trap.** `addopts=-n auto` spawns one xdist worker per core
  (15 on the dev container), each cloning `precis_test_<uuid>` plus a pool; with
  the local precis-infra stack also connected that blows `max_connections=100`
  → `PoolTimeout` on thousands of tests. Re-run with `-n 4`. `-p no:xdist`
  breaks the `-n` in `addopts` — use `-n 0` for serial, `-n N` for bounded.
