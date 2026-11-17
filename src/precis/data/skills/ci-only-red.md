---
id: ci-only-red
family: work
title: Diagnose and reproduce a CI-only red
summary: the read-then-reproduce recipe for a red CI shard that passes locally — outage triage first, attempt-scoped logs and junit artifacts, the fresh-database shard repro, the teardown probe that names the leaker, ordering flakes, the 40-minute cap and the nano seed-sweep tail
answers:
  - CI is red and I cannot reproduce it locally — what do I do?
  - a failed check has no jobs, or a suite ran zero tests — red or GitHub outage?
  - scripts/ci-failed shows nothing on a run reported red — where is the failure?
  - how do I reproduce a shard-only red?
  - a shard came back cancelled, or the run stalls at 92-95% — what is it?
  - CI dies with DeadlockDetected but the test passes alone — flake or bug?
applies-to: a red check.yml run, green locally
status: active
tags: [troubleshooting, workflow]
governs: factory
---

# ci-only-red — diagnose and reproduce a CI-only red

"CI is red and I cannot reproduce it" has one recipe: rule out an
outage, read the real failure, then reproduce a shard-only red against
a fresh database. `docs/conventions/testing.md` owns the `scripts/test`
mechanics (`--shard K/N`, the gate lanes, attempt-scoped logs, junit
artifacts); this skill owns the triage moves.

## Rule out a GitHub outage before reading logs

A failed check with **no jobs**, or a suite run reporting **zero
tests**, is an outage signature, not a red. Check
https://www.githubstatus.com first — no local repro attempt helps
while GitHub itself is down.

## Read the failure when ci-failed shows nothing

`scripts/ci-failed <run-id>` digests the failed jobs, but it reads the
latest attempt only. A re-run-to-green hides the failed attempt's
logs, so on a run reported red with an empty digest the output is one
level down, not gone:

```bash
gh run view <run-id> --json attempt                  # more than one attempt?
gh run view <run-id> --attempt <n> --log-failed      # the failed attempt's log
gh run download <run-id> -p 'junit-*' --dir scratch/junit  # every attempt's copy
```

Each attempt keeps its own `junit-<N>-<K>` artifact (v4 artifacts are
immutable, so same-named copies from different attempts coexist);
`retention-days: 7` applies — after a week even the artifact copy is
gone. Raw job logs pulled through `gh api` need
`--allow-escape-sequences` (`gh` ≥2.97 refuses the response otherwise);
`scripts/ci-failed` already passes it — bare `gh api` calls must too.

Two run shapes are not failures at all:

- **"cancelled" shard** — it hit the 40-minute job cap. A real hang
  fails by name via the 300 s pytest-timeout instead.
- **stall at 92-95%** — the tail is the nano seed-sweep cluster, not a
  wedge. Rebalance with `scripts/shard-weights` fed the CI junit
  artifacts, per `docs/conventions/testing.md` §`--shard`.

## Reproduce a shard-only red

A red that passes locally is usually a stale-database artifact: the
per-worktree `precis-test-db` container and its `precis_test` template
are long-lived, while CI starts fresh every run — a template created
weeks ago hides isolation bugs CI sees.

Recreate the worktree test DB container so the database is fresh, then
run the exact slice:

```bash
docker compose -f docker/dev/compose.yaml -p "precis-test-$(basename "$PWD")" --profile dev down -v
scripts/test --shard 4/10 -n2 -m "not nightly"
```

Use the `K/10` from the failing job's name. Keep the database
recreated until the shard's red reproduces, and distrust a local pass
run against the long-lived template — local green is not fresh-DB
green.

## Name the leaking test inside a red shard

A shard log names the failing file, not always the failing test. The
`store` fixture carries a built-in teardown probe: on a leaked
connection it fails the test by name (`test leaked N DB
connection(s)`). Set `PRECIS_TEST_LEAKCHECK` to `report` with
`PRECIS_TEST_LEAK_LOG` pointing at a scratch file and it collects
`count<TAB>nodeid` lines across a whole shard run instead — no code
change needed. For a resource the fixture does not watch, drop a
temporary teardown probe into `tests/conftest.py`, run the slice, read
the report, then delete the probe before committing.

## Reproduce an ordering flake

`DeadlockDetected` or a lock error in a test that passes alone is an
ordering flake, not a bug in the failing test. `scripts/test --random`
prints the shuffle seed; set `PRECIS_RANDOM_SEED` to that seed to
replay the order, and the teardown probe above names the test whose
leaked connection deadlocked its neighbour.

## Debug under xdist

Under xdist, `print()` output is captured per test and surfaces only
in the failing test's failure report — `-s` output is swallowed and
reaches nothing. Put the diagnostic in the `assert` message (or
`pytest.fail(...)`) so it lands in the CI log you can actually read.

## See also

- `docs/conventions/testing.md` — `scripts/test` mechanics: `--shard
  K/N`, the gate lanes, attempt-scoped logs, junit artifacts.
- `docs/conventions/shipping.md` — the ship gate and what a red leg
  means for a land in flight.
