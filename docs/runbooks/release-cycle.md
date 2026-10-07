# Build, release, dogfood, fix

Reto's operating decision: graph memory leads; programmes build in parallel.
Release cutoffs are event-driven. When the previous deployment has completed
and passed health checks, cut the next useful checked batch. Wait for work
when nothing new is ready. One coordinator, one open release, one deployment.
Dogfood runs alongside development and does not hold the entire fleet.

## Worker contract

1. Build a bounded slice in a task branch and separate worktree. Integration
   and release fixes also use worktrees; never edit or commit in primary main.
2. Integrate current main in the task branch and resolve conflicts before
   final checks. Run syntax/lint and the relevant acceptance tests; retain the
   full release gate. Migrations and `safe_fetch.py` keep their existing gate.
3. Land reviewed slices promptly. Report landed SHA, acceptance evidence,
   next dependency and a concrete dogfood. Main keeps accepting later slices
   while a release is prepared.
4. Dogfood the feature after its SHA is deployed. Record observed runtime SHA,
   input/object handles, expected/actual result and finding or gripe handles.
   Route a fix to its owner, rebuild and repeat. A held experiment stays held.

## Coordinator contract

| Stage | Evidence / invariant |
|---|---|
| Cut | Freeze a useful green main commit as `release/r<N>`; later main arrivals belong to the next release. |
| Prepare | Only requested fixes and release preparation enter the release. Fixes originate in fresh release-based worktrees and forward-merge to main immediately. |
| Gate | Check the exact current release SHA. Any additional release commit requires its own verdict. Syntax alone is insufficient. |
| Deploy | Pin that checked SHA; serialize deployment through completion and health verification. Respect the maintenance window in `/round`. |
| Verify | Successful deploy exit, `origin/prod`, runtime SHA, expected migration and registered kinds agree. A failure enters repair; it does not trigger another normal release. |
| Finish | Preserve the deployed tag, ensure release fixes reached main, retire the release, then consider the next useful batch. |
| Dogfood | Record results against deployed SHAs even if a newer release has begun. Blocking regressions take priority; ordinary findings enter their owner's next build. |

Do not wait for every programme to finish a build or dogfood before cutting.
Do not deploy an unchanged SHA just to keep the loop busy. A regression found
after retirement goes into the next repair release; do not rewrite a deployed
tag. The coordinator reports what is live, what failed and who owns the fix.

## Tooling boundary

Commands and recovery procedures: [round](../../.claude/commands/round.md).
Implementation: `scripts/round` (gate/deploy the release head, `deployed/r<N>` tag, merge-back) and `scripts/ship --release`.
With a recorded release, `round gate` and `round deploy` select its exact
remote head and one fresh complete CI certificate. Deployment journals retain
partial attempts; successful rollout awaits coordinator runtime observation
through `--confirm-runtime SHA --runtime-evidence TEXT` before retirement.
Without a release, main selection remains. Installed metadata and a rollout
receipt do not prove process reload; [deploy-assertions](../backlog/deploy-assertions.md)
owns the remaining live-process health hardening. End-to-end live acceptance
and dogfood remain required.
