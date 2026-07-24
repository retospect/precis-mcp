---
status: draft
title: Salvage failed fixer builds instead of discarding them
model: sonnet
---

# Salvage failed fixer builds instead of discarding them

## Motivation / why
In report mode, `run_tick` only `git push`es the build branch on the OK path
(`src/precis/fixer/tick.py::run_tick`); on any `NEEDS_YOU` — a genuine gate
failure (mypy, non-auto-fixable lint, non-zero `claude` exit) — it returns
without pushing, and the `finally` block unconditionally runs
`_worktree_remove`. An expensive opus build is thrown away with nothing left
to inspect or salvage.

Observed 2026-07-05: the `sandbox-run-substrate` slice built, then the quick
gate failed on `ruff format --check` and the whole build was discarded. That
specific trigger is fixed (`9ebca4cf` added `_autofix_lint`, mirroring ship's
ruff auto-fix). The underlying waste remains for *real* failures: a build
that's 90% right with one mypy error is lost, not reviewable.

## In scope
On `NEEDS_YOU` (genuine gate failure), preserve the build so it's
salvageable — a human or `/go` can finish it instead of re-spending opus
from scratch.

## Explicitly NOT in scope
- Changing the OK-path behavior (report mode already pushes + reports on
  success).
- Auto-finishing or auto-fixing a failed build beyond the existing
  `_autofix_lint` (ruff) pass.

## Acceptance criteria
A failed fixer build (`NEEDS_YOU`) leaves an inspectable artifact — either a
pushed branch or an on-disk worktree — reachable from the report, instead of
being discarded by the `finally` cleanup.

## Target + blast radius
- `src/precis/fixer/tick.py::run_tick` — the `NEEDS_YOU` return paths and the
  `finally` cleanup.
- The report emitter (`Report` construction) — needs to surface the salvage
  pointer (branch name or worktree path).
- Branch GC — whichever salvage mechanism is chosen adds artifacts that need
  a cleanup story paired with it.
- Extends ADR 0048 (fixer / sandbox-run job type).

## Open questions / decisions log
- **Push vs. on-disk worktree.** Two candidate salvage mechanisms:
  - Push the branch on `NEEDS_YOU` too (not just OK) — but pushed
    half-built branches accumulate on origin, worsening the existing
    stale-branch-cleanup residual; would need pairing with branch GC.
  - Keep the failing worktree on disk under `.fixer-work/` with a pointer
    in the report, instead of pushing.
- **Branch GC pairing.** If push-on-failure is chosen, what's the cleanup
  policy for salvaged-but-abandoned branches (age-based? explicit
  resolution?) — undecided.
