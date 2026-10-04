---
status: in-progress
pillar: platform
---
# Parallel programmes and continuous release rounds

Reto, 2026-10-04: graph memory is the leading objective. Local compute,
Meluxina ML-potential/DFT integration, PCB place/route/EWOD, printed materials,
smooth-surface carbon wrapping with Y instrumentation, SE print normalization,
research and Drive UX progress in parallel. Paper and catalysis remain equally
funded within the research work; they are not the entire programme.

## Operating decision

Each thread builds in its own branch and worktree and lands small reviewed
slices on main promptly. The coordinator freezes a useful batch into one
release branch. Only fixes and release preparation enter that branch; new
features continue on main. Deploy the checked release SHA, verify runtime
health, merge every release fix back to main and retire the release branch.
Then start the next useful release as capacity becomes available. No fixed
2–3 hour timer; no overlapping deploys and no empty releases. Dogfood runs
asynchronously against named deployed SHAs. A blocking dogfood regression
becomes a release repair; ordinary findings feed the owning thread's next build.

The existing release-branch-rounds spec owns implementation and live acceptance.
Cut, release-fix forwarding and exact-release gate/deploy are implemented;
runtime observation and end-to-end live dogfood remain required. Existing migration
and safe-fetch gates still apply. The coordinator owns deployment; workers
report build SHA, acceptance evidence, dogfood SHA/result and remaining blockers.

## Scope

- Map programmes to current owners/specs, preserve explicit research holds.
- Prefer one PCB programme with coordinated place/route, EWOD and round-trip
  lanes; shared code has one owner per change, not competing implementations.
- Distinguish printed material design from SE unit/scale normalization and
  carbon surface wrapping. Identify unfiled Meluxina compute work.
- Verify an end-to-end live release under the recorded
  [release contract](../runbooks/release-cycle.md).
- Implement the approved [Drive task entry points](drive-filter-hierarchy.md);
  preserve capability coverage and review the remaining storage/API details.
- Preserve earlier dirty worktrees; never perform task edits in the primary
  checkout. Any worker implementation uses its own branch/worktree.

## Acceptance criteria

A compact programme table names each outcome, owning threads, immediate next
slice/dependency and a concrete dogfood. The release contract says which SHA
is frozen, tested, deployed and observed. Implemented tooling is distinguished
from pending live acceptance. Drive decisions remain traceable input.
