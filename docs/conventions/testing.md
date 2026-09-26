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

The remote side leaks the same way. `scripts/ship --remote` pushes the tree to
a throwaway `ci/<branch>` for check.yml, and deletes it again only on a
completed ship — a red gate you then `/qland`, or a ship killed mid-run, leaves
the ref on GitHub forever (12 had accumulated before anyone looked).
`sweep_ci_refs` in `scripts/reap-worktrees` deletes a `ci/<X>` once no local
branch `<X>` exists *and* the ref is older than `PRECIS_CI_REF_MAX_AGE_SECONDS`
(default 24h). The age floor is the safety property, not politeness: a tree
whose work a sibling qlanded can be reaped while its own gate is still running,
and deleting that ref kills the run. Escape hatch `PRECIS_NO_CI_REF_REAP=1`
(remote untouched, worktrees still reaped); offline or unauthenticated is a
silent skip, never a hook failure.

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
scripts/test --shard 2/6             # bucket 2 of a 6-way split (what CI runs per job)
```

`--shard K/N` (tests/conftest.py) hashes every test id into one of N
buckets (crc32, so every process agrees). The N shards partition the suite
exactly; check.yml runs the Linux legs as 6 parallel shard jobs so the gate's
wall-clock is one shard, not the suite. Locally it is only for reproducing a
red CI shard — run the same `K/N` the failing job name shows.

**CI shapes (check.yml, decided by its `plan` job).** The ship gate
(`ci/**` and `main` pushes, PRs) is lint + mypy + 6 shards of Linux+db on
3.13 — 8 jobs, so two ships fan out fully under the 20-runner cap. A diff
that touches only `docs/` and root `*.md` gets the **docs lane** instead of
the shards: one job of the fast set (`-m 'not db and not slow'`, no
Postgres), which holds the doc-reading tests (doc pointers, the secrets
sweep, backlog front-matter). Skills, `scripts/` and `.claude/` are not docs
for this purpose — tests execute them. The **full** shape (adds 3.12, macOS,
Windows) runs nightly and on `workflow_dispatch` with `full: true`; a
platform-only break therefore surfaces the next morning, off the ship that
caused it — `tests/test_posix_only_guards.py` is the compile-time stand-in
for the Windows leg.

Tiers, fastest to most complete — pick by what you changed:

- **`--impacted`** is the tightest inner loop: `pytest-testmon` maps
  test↔code and runs just the tests a working-tree change touches (the first
  run builds the map; later runs are sub-second when nothing relevant
  changed). Use it while iterating on a specific edit. It also deselects
  `slow` by default (gr261537) — testmon's map doesn't know the marker, so a
  central-module diff could otherwise pull the heavy cluster into a selection
  that is *also* forced to `-n0`, making impacted mode slower than a plain
  full run. Pass your own `-m` to override.
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
  cluster (real materials/chemistry/pcb compute — see the marker in
  `tests/conftest.py`). That one cluster dominates the suite's wall-clock, so
  this is a big cut while still exercising almost every code path. As of this
  change it is also the local ship gate's default lane — so all three local
  lanes (`--impacted`, `--fast`, the gate) now agree on deselecting `slow`,
  and CI is the only place that runs it. See below.

Neither of the first two substitutes for a broad run before shipping —
testmon's map can miss an indirect dependency, and `--fast` deselects the
entire DB suite.

`scripts/ship` (via `/land`, `/go`) runs the **authoritative** pre-merge gate
(`ruff` + `mypy` + `pytest`, in-container). Everything above is the fast loop
that gets you to a green gate cheaply — the gate is what actually decides
mergeability.

**The local gate's default lane is `-m 'not slow'`.** The `slow` cluster is a
handful of tests holding a large share of wall-clock: on 2026-09-25 a full
local gate took 2h08m and spent 90+ of those minutes on the last ~5% of the
run, five of six xdist workers idle. At that duration `/go` could not win a
CAS race against a `/qland` burst — main moved underneath two consecutive
gates before either reached its squash-merge. So the local gate deselects the
cluster and `/go` gets its verdict in minutes.

What still covers it, so nothing is actually ungated:

- **check.yml's 6 Linux shards carry no `-m` filter** — every push to `main`
  and every `ci/**` pre-merge gate runs the whole cluster.
- The nightly full matrix runs it on 3.12, macOS and Windows too.

The trade, taken deliberately: a `slow`-cluster regression can reach the
cluster via `/go` and be caught by CI minutes later, rather than being
blocked before deploy. `scripts/ship --slow` (or `PRECIS_GATE_SLOW=1`)
restores the authoritative full set — use it when the change *is* in the slow
cluster's subsystem.

One interaction to know about: the full gate's diff-coverage check measures
only what the gate run executed, so deselecting the slow cluster removes
exactly the tests that cover the pcb/se/hexfold `src/` lines. A genuinely
well-tested change there can therefore fail diff-cover at 90%. `scripts/ship`
says so in the failure text; re-run with `--slow` before writing new tests.

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

## Coverage posture: diff-gated, never %-tracked

A repo-wide coverage percentage is still not a merge criterion — a big
number on a mature tree proxies nothing. What **is** gated (since
2026-08-24): **changed `src/` lines must be executed by a test.** The
full-suite ship path (`/go`, bare `scripts/ship`) runs pytest under
pytest-cov in the gate container, then `diff-cover` on the **host** (the
warm gate container has no `.git` — source arrives via `git archive`, so
the container can't compute the diff; `relative_files = true` in pyproject
makes one `coverage.xml` valid in both places). Under
`PRECIS_DIFF_COVER_MIN` (default 90) the ship dies with the untested
changed lines listed. `/land --impacted` is exempt — its testmon-narrowed
run would under-count by construction. `PRECIS_DIFF_COVER_MIN=0` is the
deliberate override; say why in the ship message. Defensive-only lines
(`TYPE_CHECKING`, `NotImplementedError`, `@overload`) are excluded via
`[tool.coverage.report] exclude_also` — extend that list rather than
sprinkling `# pragma: no cover`.

Execution is necessary, not sufficient: the real-PG-companion-test policy
above still stands (a covered line through FakeStore proves nothing about
its SQL), and the mutation pass below is what checks that covering tests
actually *assert*.

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

**Catch power — mutation, budgeted on the ship path + spot-checks on demand.**
Line coverage says a line *ran*, not that a test would *fail* if it broke —
and execute-but-barely-assert is the characteristic failure mode of
agent-written tests. Two tools:

*On `/go` (automatic, advisory):* `scripts/mutate-diff` mutates only the
just-shipped commit's **covered** changed `src/` lines and runs each mutant
against just the tests that covered that line (per-test contexts recorded by
`scripts/ship --mutate`), capped by `PRECIS_MUTATE_MAX` (20) and
`PRECIS_MUTATE_BUDGET` (600s). A sampled survivor is escalated and re-run
against the full covering set before being reported, so a plain `SURVIVED`
line is a claim about all covering tests, not the 5-test sample — a change
your tests don't notice, harvested as a residual in `/go` step 10. Only a
note ending `UNVERIFIED, may be a false survivor` (the re-run hit its budget
or timeout) needs hand-checking before filing: apply the mutation and run
the module's tests yourself.
Diff-targeting is why it stays cheap: mutmut mutates whole files and can't
use the gate's coverage contexts for test selection.

*Whole-module spot-check (manual, periodic):* when you want a deep read on
one risk-dense module rather than a diff, run mutmut over it in a dev
shell:

```
# in a dev shell (scripts/dev); pick ONE hot module, not the tree.
# mutmut 3.x takes the path positionally (v2's --paths-to-mutate is gone).
uv run --with mutmut mutmut run src/precis/<module>.py
uv run --with mutmut mutmut results        # survivors = untested behaviour
```

Survivors are the honest to-do list: each is a code change your tests don't
notice. Target the risk-dense modules (review-tier logic, SQL builders,
routing/threshold math), not everything. A surviving-mutant report is a
sharper "are these tests effective?" answer than any coverage number — the
diff pass keeps new code honest per-ship; this spot-check is for auditing
stock.

**What caught a real bug — leave a one-line trace.** When a red gate (or a
`--fast`/`--impacted` run) actually stops a real defect from shipping, note
it in the fixing commit's subject (`fix(x): … caught by test_y`) or the
test's docstring. Over months `git log --grep 'caught by'` becomes the
ground-truth map of which tests earn their runtime — no separate tracker to
maintain and rot.

## Characterization budgets — assert what you measured, not what you hoped

For anything discretisation-dependent (mesh quality, solver iteration
counts, numerical residuals), the useful assertion is the number you
actually got plus headroom, not the number you wish were true. A test
asserting an aspiration fails on arrival and gets weakened until it
passes, which leaves a test that no longer means anything; a test
asserting measured behaviour with a stated budget fails only on *drift*,
which is the event worth knowing about.

Write the measured value into the message, so a future reader can tell
the budget from the observation:

```python
budget = 0.10 if n % 2 else 0.55       # measured: odd 0-5.6%, even 18-49%
assert sliver_frac < budget, (
    f"{name} n={n}: {sliver_frac:.1%} near-degenerate -- budget {budget:.0%}. "
    "A sharp rise means the sampling tie-break or wrap indexing regressed."
)
```

Two rules that fall out of it:

- **Never `== 0.0` on a computed float.** Enforced for the numeric suites
  by `tests/test_no_float_equality_in_numeric_tests.py`. Slice 1 of
  `precis_surface` shipped a degeneracy test asserting `area == 0.0`
  that passed vacuously against triangles of area 9e-23 — twenty orders
  of magnitude below the median, and never bit-exactly zero.
- **Know which of your invariants are combinatorial.** They hold
  regardless of the geometry and therefore cannot detect a geometry bug.
  The angle-defect total of a closed triangle mesh is `2*pi*chi` for
  *any* vertex positions, so it stayed correct to 1e-13 across a bug that
  made every per-vertex defect wrong by three orders of magnitude. An
  invariant that cannot fail is not a check; pair it with one that reads
  the distribution.
