# Reset the local test DB (edited-migration checksum mismatch)

**Since gr479991 this self-heals.** `conftest.py`'s template-maintenance
step calls `Migrator.apply_all(rebuild_on_checksum_mismatch=True)`, so when
the per-worktree `precis_test` template's ledger disagrees with the files —
an edited *draft* migration's checksum, or an applied version no file
provides after a renumber — the migrator drops every user object
(`src/precis/store/migrate.py::_drop_user_objects`) and replays the chain.
One `log.warning` line, then the run proceeds. Manual resets below are
only for cases the rebuild can't reach: a non-test database, a corrupt
container, or drift the rebuild itself can't replay.

**Symptom (the old failure shape).** `scripts/test` / the ship gate used to
abort with

```
RuntimeError: checksum mismatch for already-applied migration
  '00NN_...': file has <new>, DB has <old>. Refusing to run —
  sealed migrations must not be edited.
```

after you edited a **not-yet-shipped** migration (even a comment) that the
local gate had already applied once. The migrator seals on checksum
(`src/precis/store/migrate.py::apply_all`), and the `precis-test-db` compose
service (`docker/dev/compose.yaml`) persists across runs, so the stale
checksum survived in its `public._migrations` ledger — and one refusal in
the session fixture cascaded into an error on every DB test.

**Still red? First: which DB reddened.** Recreating the wrong one looks
like the reset "didn't work" (cost two full ship cycles on 2026-08-27):

- **`scripts/test` and `scripts/ship`'s gate both use a PER-WORKTREE
  compose project** (`scripts/lib/compose-project.sh`, gr176375) —
  container `precis-test-<worktree-name>-precis-test-db-1`. A coder
  agent's run may use its own `precis-test-agent-<id>-…` project instead.
  Confirm with `docker ps | grep test-db`, then:

```
docker rm -f precis-test-<worktree-name>-precis-test-db-1
```

The next `scripts/test`/`scripts/ship` recreates it (a full replay of every
migration, so budget several minutes). Siblings' containers are untouched.
`scripts/dev` uses the same per-worktree project. Only a bare
`docker compose -f docker/dev/compose.yaml` invocation (no `-p`) lands on
the default `dev` project — container `dev-precis-test-db-1` — which the
compose commands below target.

Verify a suspect ledger directly with:

```
docker exec <container> psql -U postgres -d precis_test \
  -tAc "SELECT version, substr(checksum,1,12) FROM public._migrations \
        WHERE version LIKE '01NN%'"
```

**Fix (shared `dev` DB) — recreate the test-db container** (rebuilds the
template from baseline plus all migrations at their current checksums):

```
env UID="$(id -u)" GID="$(id -g)" docker compose -f docker/dev/compose.yaml \
  --profile dev rm -sfv precis-test-db
env UID="$(id -u)" GID="$(id -g)" docker compose -f docker/dev/compose.yaml \
  --profile dev up -d --wait precis-test-db
```

**Why this is safe.** The sealing guard is right for *shipped* migrations
(ADR 0005, forward-only) — the auto-rebuild is opt-in per call site and only
the test harness passes it; `precis migrate` still refuses. An *unshipped*
migration you are still authoring may legitimately change — you just have to
clear the local DB's memory of the old version. Best avoided by getting the
migration content final before the first `scripts/test` run.
