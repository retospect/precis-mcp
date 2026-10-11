---
status: idea
title: Test gate — an OOM-killed xdist worker fans out into ~1400 bogus errors, and real EMT pathway runs dominate a CI shard
pillar: platform
---

# Gate: OOM fan-out and slow pathway tests

Bundled in the 2026-10-11 gripe triage. Both make the gate noisy or slow
without telling the reader the real cause.

## 1. One OOM becomes ~1400 ERRORs (gr346941)

When an xdist worker is killed (OOM), xdist starts a replacement, and the
replacement loses its coverage context table. Every test it then runs ERRORs,
so one OOM shows up as about 1400 unrelated failures and hides the real
cause.

Fix direction: detect the xdist "node down" / worker-crashed event (in
`tests/conftest.py` or the `scripts/test` wrapper), fail once with a clear
message naming an out-of-memory kill and the worker, and stop the run instead
of letting the replacement run. Consider a lower `-n` for the mutation run
(`scripts/mutate`), which multiplies memory per worker.

Files: `tests/conftest.py`, `scripts/test`, `scripts/mutate`.
Who: agent-code, S.

## 2. Real EMT pathway runs dominate a CI shard (gr346717)

`tests/test_pathway_plugin.py` runs real EMT pathway computations, and they
dominate the time of the CI shard that holds the file. e2e41d5b converted 4
tests; about 12 `run_pathway_from_yaml` calls remain.

Fix direction: convert the view-only tests (those that only read the result
graph) to a synthetic fixture graph built in the test, and keep one EMT smoke
test that exercises the real `run_pathway_from_yaml` path end to end.

Files: `tests/test_pathway_plugin.py`. Who: agent-code, S.

## Acceptance

- A killed xdist worker produces one failure that says OOM, not a wall of
  errors.
- `tests/test_pathway_plugin.py` runs one real EMT computation, and the shard
  that holds it no longer dominates the CI matrix.
