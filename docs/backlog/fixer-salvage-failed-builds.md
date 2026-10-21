---
status: draft
pillar: platform
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
A failed fixer build (`NEEDS_YOU`) leaves its worktree on disk under
`.fixer-work/`, with the path in the report, instead of being discarded by
the `finally` cleanup; an OK run still removes it. Test on a fake repo.

## Target + blast radius
- `src/precis/fixer/tick.py::run_tick` — the `NEEDS_YOU` return paths and the
  `finally` cleanup.
- The report emitter (`Report` construction) — needs to surface the salvage
  pointer (worktree path).
- Extends ADR 0048 (fixer / sandbox-run job type).

## Decisions (Reto 2026-10-21)

- On `NEEDS_YOU` before ship, keep the failing worktree on disk under
  `.fixer-work/` and put its path in the report and the gripe write-back;
  push stays OK-path only, so no branch GC is needed (the next pick of the
  same branch replaces a stale worktree). Buildable now: skip
  `_worktree_remove` in `run_tick`'s `finally` for an unshipped
  `NEEDS_YOU`.

## Residuals (from OPEN-ITEMS)

Related fixer ops, batched here: stale branches to clean —
fix/smoke, fix/build-prompt-map-freshness, fix/fixer-persistent-log,
fix/launchd-smoke (origin) + fix/shippath (local), needs Reto's OK;
PRECIS_FIXER_DISCORD_WEBHOOK unset, so loud NEEDS_YOU reports are log-only;
the agentic post-deploy followup is a /readyz stub, not a real
look-at-prod-and-fix-forward pass. Owner `src/precis/fixer/tick.py`.
