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

```
scripts/test                         # full suite (-n6)
scripts/test tests/test_x.py -k …    # subset; args pass through to pytest
scripts/test --impacted              # ONLY tests your change affects (testmon)
```

`--impacted` is the tightest inner loop: `pytest-testmon` maps test↔code
and runs just the tests a working-tree change touches (the first run
builds the map; later runs are sub-second when nothing relevant changed
files touched). Use it while iterating; it is *not* a substitute for a full
run before shipping — testmon's map can miss an indirect dependency.

`scripts/ship` (via `/land`, `/go`) runs the **authoritative** full
pre-merge gate (`ruff` + `mypy` + `pytest`, in-container). Everything above
is the fast loop that gets you to a green gate cheaply — the gate is what
actually decides mergeability.

See also the `test_leak_hardfail` / `docker_wedge_test_creds` /
`test_db_shared_singleton` gotchas for specific failure modes this harness
guards against or can trip on.
