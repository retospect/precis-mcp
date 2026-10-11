---
status: idea
title: Test gate — an OOM-killed xdist worker fans out into ~1400 bogus errors
pillar: platform
---

# Gate: OOM fan-out

From the 2026-10-11 gripe triage. It makes the gate noisy without telling
the reader the real cause.

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

## Acceptance

- A killed xdist worker produces one failure that says OOM, not a wall of
  errors.
