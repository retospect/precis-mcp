---
status: idea
title: a cancelled main run + per-push lane detection leaves src ungated on main
---

# A cancelled main run leaves its src changes with no verdict

## What
On 2026-09-26 `origin/main` reached `98889951` with a **green** check.yml run
that had `test-linux` and `test-other` *skipped* — only `plan`, `lint`,
`test-docs` ran. The last main sha with a real 6-shard Linux verdict was
`9bc27528`, three commits earlier, and `git diff --stat 9bc27528 98889951`
shows 35 changed lines in `src/precis/store/_pcb_ops.py` plus a new
218-line test file. So a green main was carrying src nobody had gated.

## Why it happens
Two mechanisms compose:

1. **The lane picker scopes to the push's own delta.** On main,
   `.github/workflows/check.yml` sets `range="${BEFORE}..HEAD"` — the
   squash-merge's delta, not the delta since the last *gated* sha. That is
   correct in isolation: each push gets gated on its own change.
2. **The concurrency group cancels superseded main pushes.**
   `cancel-in-progress` is true for non-schedule events, keyed on
   workflow+ref+shape. A qland burst pushes main several times in minutes,
   so the earlier runs are cancelled.

Together: `a10708e9` and `eb6a846c` (the pushes that carried the `_pcb_ops.py`
change) were both **cancelled**, and the next push `98889951` had a docs-only
delta, so it took the docs lane and went green. Neither the code nor any
later run ever tested it. `conclusion: cancelled` is not a failure, so nothing
surfaces.

This is a superset of the `gate-lane-docs-only-trap` note in auto-memory: the
trap there is a docs-lane green being mistaken for a code gate. Here the
docs lane is the *correct* verdict for that push and main is still ungated.

## Cost seen
A `/go` deploy could not use main's green run as its gate. Recovery was a
bare `gh workflow run check.yml --ref main` — a `workflow_dispatch` sets no
`range`, so `docs_only` stays false and the full gate shape runs on the
current head. That works, but it is a manual step nobody is told to take.

## Options
- **Make the main-push range start from the last green *gate-shape* run**, not
  from `github.event.before`. Then a docs-only push that sits on ungated src
  inherits the full lane. Costs a lookup of the last successful gate run.
- Or exempt main from `cancel-in-progress` (each squash gets its verdict,
  at the cost of runner slots during a burst).
- Or have `scripts/ship --quick` refuse to qland onto a main whose last
  gate-shape run is not green/current — pairs with the pre-qland lint+mypy
  check proposed in `investigate-mypy-red-commit-bypassed-ship-gate.md`.

## Not in scope
The `ci/**` pre-merge gate — there the three-dot `origin/main...HEAD` range is
the branch's whole change, so this shape cannot occur.
