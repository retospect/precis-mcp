# Convention — running tests via `scripts/test`

**Always run tests through `scripts/test`, never a bare `pytest`/`uv run
pytest`/`scripts/dev pytest`.** It's the only invocation that reproduces the
gate `scripts/ship` runs before a squash-merge, so it's the canonical inner
loop.

## Why not a bare invocation

- **`uv run pytest` (host).** The host Python is torch-free, so it reports
  spurious `ModuleNotFoundError` for `marker`, `sentence_transformers`, … —
  not real bugs, just a missing extra the dev container bakes in. See the
  `host_pytest_paper_extra` gotcha for the specific symptom (ingest/paper/
  triage tests fail on host, pass in the container gate/CI).
- **`scripts/dev pytest`.** `scripts/dev` bind-mounts **MAIN**, not your
  worktree — you'd be testing someone else's tree. Use `scripts/test`
  instead, which mounts *your* worktree.
- **The dev image bakes all extras** (marker, sentence_transformers, torch,
  …), so no `--with`/`--extra` flag is ever needed inside it.

## What `scripts/test` actually does

Runs pytest in the dev container against your worktree (bind-mount) with
the RAM-backed test DB wired up, terse output, `-n6` parallelism by
default.

Each worktree gets its **own** Compose project (`precis-test-<worktree>`,
derived in `scripts/lib/compose-project.sh`), so its `precis-test-db` is
isolated from every sibling's `scripts/test`/`scripts/ship` gate rather than
all colliding on one shared instance — a sibling's `-n6` run can no longer
crash your gate into recovery mode (gr176375). The per-worktree project is torn
down (`docker compose -p … down -v`) when the worktree is reaped
(`scripts/hooks/session-end-reap.sh`, backstopped by `scripts/reap-worktrees`).

Both of those are coupled to a removal path, so a tree removed any other way
(the `ExitWorktree` tool, the housekeeper agent, a hand-run `git worktree
remove`, a transient `docker compose down` failure) strands its Postgres
forever. `scripts/reap-test-dbs` (SessionStart, after `reap-worktrees`) is the
state-based backstop: it walks every `precis-test-*` project, reads the
worktree path off compose's own `project.config_files` label — the project
name can't be inverted, `compose_project_for` is not injective — and tears
down the ones whose tree is gone. A tree that still exists is never touched,
so it can't race a parked session or an in-flight gate. First run reaped 13.

All gate/test containers still share **one Docker VM memory ceiling**, so
`scripts/test` and the `scripts/ship` gate take a fleet-wide **gate slot**
(`scripts/lib/gate-slot.sh`, default 2 concurrent, `PRECIS_GATE_SLOTS`
overrides) before the heavyweight container run — a queued run waits with a
message instead of OOM-killing a sibling's at random (exit 137 / silent
pytest death mid-run, gr202193). Abandoned slots are stolen when the holder
pid dies or after 45 min.

```
scripts/test                         # full suite (-n6)
scripts/test tests/test_x.py -k …    # subset; args pass through to pytest
scripts/test --fast                  # fast set (-m 'not db and not slow'), no Postgres
scripts/test -m 'not slow'           # full-minus-glacial (skips the heavy cluster)
scripts/test --impacted              # ONLY tests your change affects (testmon)
scripts/test --durations=25 …        # profile: pytest prints the 25 slowest
```

Tiers, fastest to most complete — pick by what you changed:

- **`--impacted`** is the tightest inner loop: `pytest-testmon` maps
  test↔code and runs just the tests a working-tree change touches (the first
  run builds the map; later runs are sub-second when nothing relevant
  changed). Use it while iterating on a specific edit.
- **`--fast`** runs the **fast set** — `-m 'not db and not slow'`: skips every
  test that touches a `store`/`hub` fixture (auto-marked `db` in
  `tests/conftest.py`) *and* the heavy `slow` compute cluster. Both exclusions
  matter — the two slowest tests in the whole suite are no-DB compute tests in
  the `slow` file, so `not db` alone would let them leak in. No Postgres is
  started, so it's Docker-DB-independent and finishes in seconds. It's the
  right gate for a change that can't touch a DB path — pure logic, formatting,
  docs, config, a CLI-arg parser. It is a *coverage subset by construction*: a
  change to any SQL/store path is exactly what it can't see, so it never
  substitutes for the full run there.

- **`-m 'not slow'`** keeps the DB suite but drops the `slow`-marked heavy
  cluster (real materials/chemistry compute — see the marker in
  `tests/conftest.py`). That one cluster is ~65% of the suite's wall-clock, so
  this is a big cut while still exercising almost every code path. Use it when
  you want broad DB coverage without paying for the glacial compute tests.

None of these substitute for a full run before shipping — testmon's map can
miss an indirect dependency, `--fast` deselects the entire DB suite, and
`-m 'not slow'` skips the heavy cluster. The ship gate (and `/go` before a
deploy) always runs everything.

`scripts/ship` (via `/land`, `/go`) runs the **authoritative** full
pre-merge gate (`ruff` + `mypy` + `pytest`, in-container). Everything above
is the fast loop that gets you to a green gate cheaply — the gate is what
actually decides mergeability.

## Raw SQL ⇒ a real-PG test — FakeStore is blind to SQL

**Any route or handler that builds/executes raw SQL must have at least one
test that runs that SQL against real Postgres.** The FakeStore doubles in
`tests/_fakes.py` return canned rows without parsing SQL, so they pass
happily over a broken query — wrong paramstyle, a literal `%` in a
parameterized `LIKE` (500s on real psycopg, invisible to FakeStore — the
`psycopg_percent_like_fakestore_gap` gotcha), a column renamed out from
under a string literal.

The shape: a `tests/precis_web/test_<module>_sql.py` companion using the
real store fixture — `test_status_sql.py`, `test_tags_sql.py`,
`test_smartdraft_sql.py` are the precedent. It doesn't need to re-test the
route's logic (FakeStore tests keep doing that cheaply); it needs to
*execute every raw query at least once*, including with adversarial input
(`%`, `_`, quotes) anywhere user text reaches a pattern.

When review or a new route adds raw SQL with FakeStore-only coverage,
that's a gap to fix in the same change, not a follow-up.

See also the `test_leak_hardfail` / `docker_wedge_test_creds` /
`test_db_shared_singleton` gotchas for specific failure modes this harness
guards against or can trip on.

## Coverage posture: deliberately unmeasured

No coverage tool is wired into `scripts/test`/`scripts/ship` — this is a
decision, not an oversight. The gate is `ruff` + `mypy` + `pytest`; a
coverage percentage is not a merge criterion and never gates a ship. The
risk class coverage numbers are meant to proxy for — SQL paths FakeStore
can't see — is already covered directly by the real-PG-companion-test
policy above, which is a sharper signal than a line-coverage threshold.

## Judging effectiveness (instead of a coverage %)

A big suite (~11k tests) earns its keep only if it *catches* things and
doesn't just *cost* things. Two cheap, honest signals — neither is a gate,
both are periodic:

**Time sink — profile, don't guess.** `--durations=N` is already wired
through (`scripts/test --durations=25`). Run it when the suite feels slow and
read the tail. The dominant cost is per-DB-test setup (template-clone +
per-test `TRUNCATE`), so the usual finding is a test that pulls a
`store`/`hub` fixture but only asserts pure logic — it pays for Postgres it
never uses. Fixing those is the highest-leverage runtime win: swap
`store`/`hub` → `hub_stateless`/`runtime_stateless` (no-DB fixtures in
`tests/conftest.py`). That both shrinks the slow suite *and* moves the test
into the `--fast` set. Don't delete tests to go faster; re-home the
mis-classified ones.

**Catch power — mutation-spot-check, don't count lines.** Line coverage says
a line *ran*, not that a test would *fail* if it broke. When you want a real
read on whether a module's tests assert or merely execute, run a mutation
pass over that one module (periodic, never in the gate):

```
# in a dev shell (scripts/dev); pick ONE hot module, not the tree.
# mutmut 3.x takes the path positionally (v2's --paths-to-mutate is gone).
uv run --with mutmut mutmut run src/precis/<module>.py
uv run --with mutmut mutmut results        # survivors = untested behaviour
```

Survivors are the honest to-do list: each is a code change your tests don't
notice. Target the risk-dense modules (review-tier logic, SQL builders,
routing/threshold math), not everything. A surviving-mutant report is a
sharper "are these tests effective?" answer than any coverage number, which
is why it lives here and not in the gate.

**What caught a real bug — leave a one-line trace.** When a red gate (or a
`--fast`/`--impacted` run) actually stops a real defect from shipping, note
it in the fixing commit's subject (`fix(x): … caught by test_y`) or the
test's docstring. Over months `git log --grep 'caught by'` becomes the
ground-truth map of which tests earn their runtime — no separate tracker to
maintain and rot.
