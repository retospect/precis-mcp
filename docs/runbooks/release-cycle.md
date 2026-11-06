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
| Good | After ~24h clean runtime (no new alerts attributed, doctor green), the sweep tags `good/r<N>` on `deployed/r<N>` via `scripts/round good`. Rollbacks target the newest `good/r<N>` (`round good --resolve`), never a guessed `deployed/r<N-1>`. |
| Dogfood | Record results against deployed SHAs even if a newer release has begun. Blocking regressions take priority; ordinary findings enter their owner's next build. |

Do not wait for every programme to finish a build or dogfood before cutting.
Do not deploy an unchanged SHA just to keep the loop busy. A regression found
after retirement goes into the next repair release; do not rewrite a deployed
tag. The coordinator reports what is live, what failed and who owns the fix.

## Factory loop

A second path to the same coordinator contract: when armed, the factory
release loop (td570415) runs the stages below unattended through
`plan_dispatch`, instead of a human driving `/round`. Detail on each stage:
`docs/runbooks/dark-factory-arming.md` §Release pass, §Standing approval,
§Stuck watchdog.

| Stage | Op | Who answers on a failure |
|---|---|---|
| Cut | `release_cut` | retry cap exhausted → a human ask |
| Gate | `ci_verdict_poll`; a red verdict runs `ci_red_digest` → fix → `release_cut` recut, at most 2 automatic loops | the second red files a ruling ask; a third fix is a human call |
| Approval | the `approval` ask | `release.auto_deploy` auto-answers `deploy r<N>` on a green verdict for the cut's exact sha; otherwise a human answers |
| Deploy | `release_deploy` | retry cap exhausted → a human ask |
| Confirm | `release_confirm`: mechanical runtime attestation | a mismatch or gap fails the job and asks a human; there is no rollback |
| Stuck watchdog (any stage) | `release_poll`'s pass | files one ruling ask per `(round, stage)` once `release.stage_deadlines` is exceeded — observation only; answering it executes nothing |

These always stay with a human or the coordinator, armed or not:

- Rollback, and the `good/r<N>` tag (`scripts/round good`).
- Superseding a partial rollout: `scripts/round supersede --partial --reason "<why>"`.
- Deleting a remote release branch.
- A red fix loop past the automatic cap.
- A failed runtime probe (a `release_confirm` mismatch or gap).
- Any stuck-release ruling ask — answering it executes nothing.

### Before the first factory-run release (open, read 2026-11-06 19:45Z)

None of this is done. Delete this subsection once a supervised factory
release has run once.

**Blocks.**

1. No `release_poll` tick todo exists yet (arm shape:
   `docs/runbooks/dark-factory-arming.md` §Release loop). Check its
   schedule form against an existing recurring row before arming — prod
   rows store `cron`, not the runbook's `every` example.
2. Origin carries a stale `release/r221` head with no `deployed/r221`
   tag, so `release_poll`'s in-flight check (`release_poll::_in_flight`)
   reports it in flight forever. Removal is a remote branch delete
   (`git push origin --delete release/r221`) — the owner's to approve.
3. An infra-caused red CI run is not told apart from a test red
   (`ci_verdict_poll::verdict_of`, `ci_red_digest::_digest_done`), so one
   would spend one of the two automatic fix iterations; a shared
   classifier is ruled into td574946 S6, not built yet.
4. `release.round` must hold no open round before arming — a hand
   release (r233) was open on 2026-11-06Z.

**Arming order, after the blocks clear.** Probe the token (a
`ci_red_digest` job with `{probe: true, run_id: <recent check.yml run>}`;
Actions read access is unproven, td573240); set `release.repo
<owner>/<repo>`; decide `release.no_release_windows` and
`release.health_url`; run a `release_poll` job with `{dry_run: true}`
(expect "would mint"); arm the tick todo; watch cut, verdict and
approval on `/asks` (answer by hand, `answer: deploy r<N>`, then tag the
ask done), deploy, and confirm — the evidence line names every live
host, `deployed/r<N>` exists, the root closes. If `release.auto_deploy`
is to go on next, require a second dry run that blocks at "main not
ahead" first, that the heartbeat race did not trip, and that nothing
hand-edited the release branch meanwhile.

**Stop procedure.** Pause the tick todo and set `release.auto_deploy
false`; halt a minted pass by answering `hold` on its approval. A
stopped pass leaves the round open: finish it by hand with
`scripts/release --adopt` or `scripts/round cut --abandon`, then cancel
the pass root.

**Known gaps.** The loop never commits to the release branch — a fix
lands on main and recuts r<N+1>. If `release.auto_deploy` is on, a
hand-moved release head is refused by `_open_round_record`.
`release_confirm` has zero margin for a worker heartbeat still at the
old sha just after a rollout finishes — a false trip files an ask, not
a bad deploy. Re-creating the session MCP and `fleet say` stay manual.

**Not verified.** The worker's GitHub token scope and rate limit;
whether the cut host's slice account can run `scripts/deploy`; CI
duration against the verdict poll's park cap (about 60 minutes per
attempt, 3 retries) against a roughly 90-minute full gate.

## Tooling boundary

Commands and recovery procedures: [round](../../.claude/commands/round.md).
Implementation: `scripts/round` (gate/deploy the release head, `deployed/r<N>` tag, merge-back) and `scripts/ship --release`.
With a recorded release, `round gate` and `round deploy` select its exact
remote head and one fresh complete CI certificate. Deployment journals retain
partial attempts; successful rollout awaits coordinator runtime observation
through `--confirm-runtime SHA --runtime-evidence TEXT` before retirement —
that is the hand path. When the factory is armed, `release_confirm` attests
the same release's runtime mechanically instead, with no rollback.
Without a release, main selection remains. Installed metadata and a rollout
receipt do not prove process reload; [deploy-assertions](../backlog/deploy-assertions.md)
owns the remaining live-process health hardening. End-to-end live acceptance
and dogfood remain required.
