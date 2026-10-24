---
status: draft
title: scripts/test: single-file and small-selection runs in seconds, not 18–40 s
pillar: platform
prio: high
---

# scripts/test: single-file and small-selection runs in seconds, not 18–40 s

Source: gr514478 (keeper: a single-file `scripts/test` costs 25–90 s).
Folded in: gr514483 (a named no-DB file still queues and starts Postgres),
gr514502 (the run backgrounds after ~30 s of silence), gr514875
(`uv run --frozen python -c` ~17 s on the host), gr514982 (~1 min of
"bringing up nodes" for ~10 s of pytest). Ties to gr514466 (Reto
2026-10-24: a stopgap now, then RAM-based tests with read-only access to
the main DB). Reto ruled on 2026-10-24 that this theme goes first.

## Motivation / why

Every builder in the fleet runs `scripts/test <one file>` many times per
task. Five gripes on one day describe the same cost from different angles.
The measurement below shows the time is not where the gripes place it.
Container bring-up and Postgres are cheap. The cost comes from three places:
xdist `-n6` on a one-file selection, a ~6 s preflight that runs every
time, and a ~4.5 s per-process test-DB session setup that `-n6` pays six
times. The "bringing up nodes" line quoted in gr514478 and gr514982 is
pytest-xdist starting its workers. It is not Docker.

## Measured (2026-10-24)

Host: Docker VM 47 GiB / 12 CPUs (`docker info`), 89 containers running,
1 of 3 gate slots held, so slot wait was 0 s. Image `precis-dev` built 5 h
earlier. Files: `tests/test_angle.py` (13 tests, no DB) and
`tests/test_acting_user.py` (14 tests, DB). The scripts are under
`/tmp/tls-measure/` and are not in the repo. Each run was timed with phase
lines stamped from `bash -x scripts/test`, plus each phase run alone in the
same compose project. Repeat runs varied by ±2 s.

End to end (`PRECIS_TEST_KEEP_DB=1`, warm DB):

| run | default (`-n6`) | `-n0` | `--fast` |
|---|---|---|---|
| no-DB file | 17.9 s (13.0–35.6 over 5 runs) | 11.0 s | 14.5 s |
| DB file | 40.6 s (46.9–47.3 in run 1) | 17.5 s | n/a |
| DB file, cold DB (agent tree after `down -v`) | — | 19.4 s | n/a |

Where the time goes (single file, warm):

| phase | no-DB file | DB file | lever |
|---|---|---|---|
| disk preflight, compose config | 0.2 s | 0.2 s | none |
| `compose up --wait precis-test-db` | 0.7 s warm / 2.8 s cold | same | S4 skips it for no-DB |
| gate slot | 0 s here; minutes under fleet load (gr514466) | same | S4 skips it for no-DB |
| `compose run` create + start + rm | 0.3–0.6 s | same | none needed |
| `bash -lc` + `uv run --no-sync` | ~0.2 s | same | none needed |
| `check-core-deps.py` (imports every core dep) | 5.5 s | same | S2 |
| `check-lock-pins.py` + `check-entry-points.py --heal` | 0.8 + 0.5 s | same | S2 |
| pytest start: interpreter, conftest, plugins, collect | ~2.5 s (`--co` 0.7 s of it) | same | none in scope |
| test-DB session setup (`_initialise_test_db`), per process | 1–1.5 s, runs even with no DB test selected | 4.5 s, cold or warm template | S3, S5 |
| xdist `-n6` worker bring-up | +4.8 s (6.45 s in pytest vs 1.69 s at `-n0`) | +24 s (31.4 s vs 7.3 s) | S1 |
| the tests themselves | 0.7 s | ~2.5 s | — |

Other measurements:

- **Session setup.** The 4.5 s is the same on a fresh DB (`down -v`, so
  all 240 migrations apply) and on a warm template (from `--durations`:
  4.74 s vs 4.47 s). So it is not migration cost. It is a fixed cost per
  pytest process. Under `-n6` each worker pays it and they queue on the
  advisory lock (`_SESSION_DB_LOCK_KEY`), which fits the 16.4 s
  first-test setup seen at `-n6`.
- **A warm long-lived container gains nothing.** `docker exec` of the same
  pytest into a running `precis-dev` took 3.5 s, the same as
  `compose run --rm` (3.5 s). Container create is 0.3–0.6 s.
- **Host `uv run` (gr514875) did not reproduce.** The first call in a fresh
  tree took 1.2 s, including creating `.venv` and building the editable
  install. Later calls took ≤0.1 s, with or without `--frozen` or
  `--with`. `scripts/test` does not call host `uv run` on its hot path.
  Only `--selected` does, through the `scripts/impacted-tests` shebang
  (0.5 s).
- **Suite shape.** 15,014 of 42,437 tests are `db` (35 %). Collecting the
  whole suite takes ~95 s, so whole-suite collection cannot be used to
  classify a run. Collecting one file takes 0.7 s.
- **Gate slots.** The `gf-gateslots` branch (489f9de03, e41255957,
  unlanded) derives the cap as
  `min(floor((Mem − 16 GiB) / 4.5 GiB), NCPU / 2)`. On this VM that is
  min(6, 6) = 6, against today's flat 3. With that branch, `--fast` runs
  skip the queue. Named-file runs still queue (gr514483).

## Options considered

| lever | saves (single file) | verdict |
|---|---|---|
| `-n0` for small selections | 4.8 s no-DB, ~23 s DB | **S1** |
| cache the three preflight checks | ~6 s every run | **S2** |
| no session DB setup when no `db` item is collected | 1–1.5 s, and per xdist worker | **S3** |
| skip Postgres and the gate slot for no-DB selections | 0.7–2.8 s, plus all slot wait | **S4** |
| make per-process session setup cheap (fingerprint, clone only) | ~3.5 s per DB process | **S5** |
| heartbeat while silent | 0 s; stops harness backgrounding (gr514502) | **S6** |
| read-only main-DB fixture (gr514466) | n/a; adds real-data tests | **S7** |
| warm long-lived test container | ≤0.6 s (measured) | rejected: exec = run |
| tmpfs pgdata | ≤1 s; fsync, full_page_writes and synchronous_commit are already off | deferred: cold `up` is container start, not IO |
| template DB instead of migrating | already done (FILE_COPY clone per session) | S5 trims what is left |
| in-process store instead of Postgres (gr514466 ask) | removes Postgres from DB runs | not now: Questions 1 |
| avoid host `uv run` | 0.5 s on `--selected` only | not reproduced; Questions 4 |

Expected after S1–S5: no-DB file 17.9 s → ~4 s, and it never queues. DB
file 40.6 s → ~9 s.

## Decisions

1. **Small selections run serial.** When the caller passes no `-n`, every
   positional argument is a test file or node id (no directories, no bare
   `-k` over the suite), and there are at most 3 distinct files, use
   `-n0`. Otherwise keep `-n6`. `--impacted` already forces `-n0`.
2. **Preflight results are cached, not dropped.** The three checks guard
   against a stale baked venv (gr286507, gr453828, gr494988), which is a
   real failure. They run when the key changes and are skipped on a hit.
   The key is: image id, sha256 of `uv.lock`, sha256 of `pyproject.toml`,
   and `UV_WITH`. The stamp is per-worktree (S2). `scripts/ship` keeps
   running them every time.
3. **DB classification is static, conservative and checked.** A host-side
   stdlib scan decides whether the named files can reach a DB fixture. If
   it is unsure, the answer is "needs DB". If a run classified no-DB
   collects a `db` item, that test errors and names the fixture and the
   override flag. It is not skipped: the current auto-skip on an
   unreachable DB would turn a misclassification into a false green.
4. **No-DB runs take no gate slot.** This extends the `--fast` rule from
   the `gf-gateslots` branch. A `-n0` no-DB run peaks well under the
   4.5 GiB the slot formula budgets for.
5. **Tests never write to prod.** Main-DB access is only through the
   `agent_ro` role, with `default_transaction_read_only=on` in the
   connection options as a second guard. It is opt-in and never runs in
   CI. Coordinates come from the gitignored overlay
   (`scripts/lib/pgb-host.sh`), never from the repo, which is public.

## Slices

| # | what | target (warm, uncontended) | blocked-by |
|---|---|---|---|
| 1 | `-n0` for ≤3-file selections | DB file ≤18 s (from 40.6); no-DB ≤12 s | — |
| 2 | cache the preflight checks | −5 s per run; stamp hit ≤0.3 s | — |
| 3 | lazy session DB setup | no-DB file with DB up: no `CREATE DATABASE` | — |
| 4 | no-DB selections: no Postgres, no slot | single no-DB file under 5 s; 0 s queue at full slots | gf-gateslots lands |
| 5 | cheap per-process DB session | session setup ≤1 s; DB file under 10 s | 1 |
| 6 | heartbeat | no silent gap over 15 s | — |
| 7 | read-only main-DB fixture | opt-in `prod_ro` tests run; writes refused | Questions 2 |

### 1. Serial for small selections

Files: `scripts/test` (the `NPROC` block). The rule follows Decision 1.
Print one line, `small selection: -n0 (pass -nN to override)`.

Tests: `tests/test_scripts_test_env.py`, alongside its stubbed-compose
cases:
- one file gets `-n0`;
- four files get `-n6`;
- a directory gets `-n6`;
- `-n2` passes through unchanged;
- `--impacted` stays `-n0`.

Target: `scripts/test tests/test_acting_user.py` ≤18 s warm. Re-measure
with the method above.

### 2. Cache the preflight checks

Files: `scripts/test` (the `run_pytest` inner command),
`scripts/lib/check-core-deps.py`, `check-lock-pins.py` and
`check-entry-points.py` (unchanged unless the cache needs one shared
helper). Host side computes the key (Decision 2) with
`docker image inspect precis-dev --format '{{.Id}}'` plus `shasum`. The
key is passed into the container as `PRECIS_PREFLIGHT_KEY`. The stamp is
`/app/.precis-preflight` (worktree root, gitignored; the git dir is not
mounted in the container). The inner command skips the checks when the
stamp holds the key. Otherwise it runs them and writes the stamp, only on
success.
`check-entry-points.py --heal` patches only the throwaway container, so a
heal never writes the stamp. The next run checks again.

Tests: a new `tests/test_scripts_test_preflight.py` with stubbed docker
and compose:
- a key miss runs the checks and writes the stamp;
- a hit skips them;
- touching `uv.lock` misses;
- a failed check leaves no stamp;
- a heal leaves no stamp.

Target: second consecutive run of a no-DB file is 5 s faster than the
first.

### 3. Lazy session DB setup

Files: `tests/conftest.py` (`_initialise_test_db`). Move the clone behind
the first `db` item: either a flag set in `pytest_collection_modifyitems`,
or a session fixture that the `db` fixtures depend on instead of
`autouse`. Under xdist, check per worker whether any of its items are
`db`. Keep `_ACTIVE_DSN` semantics for every caller.

Tests: `tests/test_conftest_leakcheck.py` or a new
`tests/test_conftest_lazy_db.py` (pytester). Cases:
- a no-DB session with a reachable DB creates no `precis_test_*` database
  (count before and after);
- a DB session creates exactly one per process and drops it.

### 4. No-DB selections skip Postgres and the gate slot

Blocked by the `gf-gateslots` branch landing (it moves slot acquisition
ahead of `compose up` and adds the `--fast` skip; this slice extends it).

Files:
- `scripts/lib/needs-db.py` (new, stdlib, run as `python3 -I`). Input: the
  positional args. Output: `db` or `nodb` plus a reason. It walks each
  file's AST and its conftest chain up to `tests/conftest.py`. It builds
  the fixture-dependency closure over fixtures defined in those files and
  answers `db` if any test reaches `store`, `hub`, `hub_no_embedder`,
  `runtime_with_store` or `fresh_db`. That is the same set as
  `pytest_collection_modifyitems` (keep one list, imported or asserted
  equal by a test). It answers `db` (unsure) on any of: `usefixtures`,
  `getfixturevalue`, `pytest_plugins`, `indirect=`, a directory argument,
  or no path arguments.
- `scripts/test`: when the answer is `nodb`, skip `compose up`, skip
  `gate_slot_acquire`, and pass `PRECIS_TEST_NO_DB=1`. Add `--db` to force
  the DB path. Print one line,
  `no-DB selection (needs-db: <reason>) — no Postgres, no gate slot`.
- `tests/conftest.py`: under `PRECIS_TEST_NO_DB=1`, a collected `db` item
  gets a failing marker whose message names the fixture and says
  `re-run with scripts/test --db`.

Tests:
- `tests/test_needs_db.py` covers: a direct `store` parameter; a local
  fixture that takes `hub`; a conftest fixture chain; `usefixtures`;
  `getfixturevalue`; a directory; a pure-logic file → `nodb`;
  `tests/test_angle.py` → `nodb`; `tests/test_acting_user.py` → `db`.
- A parity test: run `needs-db.py` on 50 sampled test files and compare
  with in-container `--co -m db`. No `nodb` answer may contain a `db`
  item. False `db` answers are allowed and counted.
- `tests/test_scripts_test_env.py`: a `nodb` run makes no `compose up`
  call and takes no slot.

Targets:
- `scripts/test tests/test_angle.py` under 5 s warm after S2.
- With every gate slot held by a sleeping stub, the same run still
  finishes under 5 s.

### 5. Cheap per-process DB session

First, measure what the 4.5 s is made of: `apply_all` on an up-to-date
template, `_truncate_data_tables`, `_ensure_vocab_seeds`, the backend
terminate, and the `CREATE DATABASE … STRATEGY = FILE_COPY` clone. Record
the breakdown in this file. Then remove the dominant step. Likely: stamp
the template with a migration-set fingerprint (`COMMENT ON DATABASE`:
sha256 over migration names and checksums). When the stamp matches, skip
`apply_all`, the truncate and the seeds; the template is data-free by
construction. On a mismatch, take the existing rebuild path (gr479991).
If the clone itself dominates, compare `WAL_LOG` with `FILE_COPY` on this
small DB.

Files: `tests/conftest.py` (`_initialise_test_db`,
`_claim_template_maintenance`), possibly `src/precis/migrations` runner
helpers (read-only use).

Tests:
- a pytester case: a second session against the same template skips
  `apply_all`;
- adding a draft migration file changes the fingerprint and rebuilds;
- per-session isolation still holds (two sessions get two clones).

Target: first-test setup ≤1 s at `-n0`.
`scripts/test tests/test_acting_user.py` under 10 s warm.

### 6. Heartbeat (gr514502)

Files: `scripts/test`. A background loop prints
`scripts/test: <phase> … <N>s` every 10 s while no output has been
written. Phases: db up, slot wait, preflight, pytest start. The loop is
killed in `cleanup()`. No change to pytest output.

Tests: `tests/test_scripts_test_env.py` with a stub compose that sleeps
25 s: at least two heartbeat lines, and none after exit.

### 7. Read-only main-DB fixture (gr514466)

Files:
- `tests/conftest.py`: a `prod_ro` marker and a `prod_ro_conn` fixture.
  Skipped unless `-m` names `prod_ro`, the same opt-in shape as `mp_live`.
- `scripts/test --prod-ro`: builds the DSN at run time from the overlay
  via `scripts/lib/pgb-host.sh`. The role is `agent_ro`. The options are
  `-c default_transaction_read_only=on -c statement_timeout=30s`. The DSN
  is forwarded as `PRECIS_TEST_RO_DSN` and never written to disk.
- `docs/conventions/testing.md`: document when a test may use it (real
  data shapes, read-only queries), and that no lane runs it by default.

Tests: the fixture's own self-test:
- `INSERT` into a scratch table raises `ReadOnlySqlTransaction` or
  permission denied;
- `SET default_transaction_read_only=off` followed by a write still fails
  (the role guard);
- `tests/test_deploy_tree_no_secrets.py` stays green.

## Explicitly NOT in scope

- Replacing Postgres with an in-process store for `db` tests (Questions 1).
- The full-suite and ship-gate paths (`scripts/ship`, check.yml shards).
  They keep `-n6`, every preflight check and the DB.
- Pytest import and collection speed (~2.5 s): plugin load and conftest
  imports.
- tmpfs pgdata, and any change to the `precis-test-db` service config.
- The gate-slot cap formula itself (the `gf-gateslots` branch owns it).
- Writable prod or dev-DB access from tests.

## Acceptance criteria

Measured with the method in "Measured" (warm image, warm tree, slot free
unless stated), on the same two files:

1. `scripts/test tests/test_angle.py`: under 5 s. With all gate slots
   held by other runs: still under 5 s, and no Postgres container is
   started for the tree.
2. `scripts/test tests/test_acting_user.py`: under 10 s.
3. `scripts/test tests/` (directory) and `scripts/test` (full suite) keep
   `-n6`, the checks and the DB, and pass as before.
4. A file whose test reaches `store` only through a conftest fixture chain
   is classified `db`. A forced misclassification errors with
   `re-run with scripts/test --db` and is never skipped green.
5. No run goes 15 s without output.
6. A `prod_ro` test cannot write: both guards are tested. No hostname or
   IP appears in the diff (`tests/test_deploy_tree_no_secrets.py`).
7. `docs/conventions/testing.md` and the `scripts/test` header/usage
   describe the small-selection, no-DB and `--prod-ro` behaviour.

## Questions

1. **In-process store (gr514466).** 35 % of tests (15,014) are `db`, and
   many test SQL behaviour: migrations, triggers, constraints, pgvector
   search. An in-memory store would be a second `Store` implementation
   kept in step with the SQL one. After S1–S5 a DB file costs ~9 s, with
   ~1 s of that in Postgres. Proposal: drop the in-process store and keep
   Postgres, made cheap. Reto to confirm or overrule.
2. **Prod read path for S7.** Tests run in a container on this Mac.
   Reaching prod needs the container to route to the prod pooler.
   `scripts/prod-psql` today goes over ssh, not a direct connection.
   Options: (a) a direct `agent_ro` connection from the container if the
   tailnet routes; (b) an ssh tunnel opened by `scripts/test --prod-ro`;
   (c) a nightly read-only `pg_dump` subset restored into a local
   read-only DB (a snapshot, no prod connection at test time). Which?
3. **Small-selection threshold.** "At most 3 files" is a guess. With S5 a
   DB worker costs ~1 s, so `-n6` breaks even at roughly 30+ DB tests.
   Should the rule count collected items (costs a collection pass)
   instead of files?
4. **gr514875 (host `uv run` ~17 s)** did not reproduce (1.2 s cold, ≤0.1 s
   warm). Likely a concurrent cache lock or a re-lock after a `uv.lock`
   change. Proposal: close it as not reproduced, and ask the next reporter
   for `uv run -v` timing.
