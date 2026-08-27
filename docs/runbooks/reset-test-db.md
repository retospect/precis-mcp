# Reset the local test DB (edited-migration checksum mismatch)

**Symptom.** `scripts/test` / the ship gate aborts with

```
RuntimeError: checksum mismatch for already-applied migration
  '00NN_...': file has <new>, DB has <old>. Refusing to run —
  sealed migrations must not be edited.
```

after you edited a **not-yet-shipped** migration (even a comment) that the
local gate had already applied once. The migrator seals on checksum
(`src/precis/store/migrate.py::apply_all`), and the `precis-test-db` compose
service (`docker/dev/compose.yaml`) persists across runs, so the stale
checksum survives in its `public._migrations` ledger.

**First: which DB reddened?** There are two, and recreating the wrong one
looks like the reset "didn't work" (cost two full ship cycles on 2026-08-27):

- `scripts/test` / `scripts/dev` use the shared `dev` compose project —
  container `dev-precis-test-db-1`. That is the one the commands below
  target.
- **`scripts/ship`'s gate uses a PER-WORKTREE project** — container
  `precis-test-<worktree-name>-precis-test-db-1`. A checksum mismatch in a
  *ship gate* lives there, and the `dev` reset below will not touch it.
  Confirm with `docker ps | grep test-db`, then:

```
docker rm -f precis-test-<worktree-name>-precis-test-db-1
```

The next `scripts/ship` recreates it (a full replay of every migration, so
budget several minutes). Verify a suspect ledger directly with:

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
(ADR 0005, forward-only). An *unshipped* migration you are still authoring may
legitimately change — you just have to clear the local DB's memory of the old
version. Best avoided by getting the migration content final before the first
`scripts/test` run.
