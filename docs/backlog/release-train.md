# Release train: a release branch that holds still (gr653036)

Ruled by Reto 2026-11-12 in the coordinator session. Replaces the
re-cut loop of `docs/backlog/dark-factory.md` § "4. Release pass,
event-driven"; folds gr653036 and gr653037.

## Bottom line

A release is cut once and never re-cut. Its failures are fixed **on the
release branch**: re-run only the failed tests and their surround until
they pass, then one full run, then deploy the release head. After the
deploy starts, the release branch is merged into main with a real merge
commit, and the next release is cut from main's tip.

Why: today a red cut is fixed on main and re-cut from main's tip. Main
moves while the fixes land, so a re-cut waits for every new land's review
(the gr611217 livelock), and a fresh pass can cut below the fixes
(r292, 2026-11-12, red on the same 12 tests as r291). Prod sat on one sha
for about ten hours. A target that holds still removes both.

## Ruled design

### Flow

1. **Cut.** `release_cut` cuts `release/r<N>` from origin/main's tip, as
   today's first cut does. Only one release is open at a time.
2. **Full run.** The gate (check.yml on the release branch) runs the
   whole suite and collects every failure.
3. **Fix rounds, on the branch.** On red, the digest
   (`ci_red_digest`) groups the failures into clusters (by test file,
   then by failing module) and mints **one fix todo per cluster**, run in
   parallel. Each fixer branches from `release/r<N>` and lands onto
   `release/r<N>`, never main. Fix forward only: no skip, no xfail
   without a waiver (`precis.plans.red_waiver`).
4. **Targeted re-run.** When every fix of a round has landed, re-run only
   the failed test ids plus their surround: the tests the spot selector
   picks for the files the round changed (the same selection
   `land_slice` uses for `--checked`). Red → back to 3 with the
   remaining failures.
5. **Full run on the fixed head.** Required before deploy, never
   skipped: a fix can break a test that was green.
6. **Deploy** the release head (`deploy r<N>`, the approval ask and
   `release.auto_deploy` as today).
7. **Merge back.** As soon as the deploy starts, merge `release/r<N>`
   into main with a real merge commit (`Merge release/r<N> fix forward`,
   as r271 and r275 were by hand). The release head becomes an ancestor
   of main, so every `sha-in-prod` gate keeps reading plain git ancestry.
   Hunks main already has (the same fix landed twice) resolve to main's
   side by content (`land_slice`'s already-landed-by-content proof); a
   real conflict mints one builder todo that resolves it on a branch off
   main. Main does not require linear history.
8. **Next cut** from main's tip once the merge-back is on main and the
   deploy is confirmed (`release_confirm`).

### Reviews hold the deploy, not the cut

The engine-review gate covers the lands **inside** the release (the cut's
range) plus the fixes on its branch, and holds step 6, not step 1. Lands
after the cut belong to the next release and hold nothing. A FIX verdict
on a land inside the release mints its fix onto the release branch like
any other fix (step 3). This retires the tip-review re-cut hold
(`release_poll.note_cut_hold` kind `tip`) and the cut freeze
(`release_poll.land_freeze`, gr651885): with no re-cut there is nothing
to freeze for.

### No abandon; an alarm instead

A fixed target is always worth finishing, so the pass never drops a
release for a fresh cut. The coordinator ask fires when:

- a fix round ends with no fewer failing tests than it started with;
- the release is still red two hours after its cut;
- the failure is not code (runners down, network, an expired
  credential): at once, naming the class.

The ask lists, per failing test, any main commit since the cut that
touched it, so the coordinator can cherry-pick a fix main already has
onto the release branch. Answering it never closes the release. The
2-iteration loop cap (`_RELEASE_LOOP_MAX`) goes; progress is the limit.

### Holds ask at once

A hold that cannot clear itself (a FIX whose fix ended without a land, a
reviewer under a dead root) files its ask on the next poll, not after an
hour (gr653037 a). `plan_dispatch` routes a brief to the coordinator for
a deny path only when the brief asks to edit that path, not when it cites
it as checked (gr653037 b).

### Timing target

Green within about three hours of the cut: full run ~90 min, a fix round
(builder plus targeted re-run) 20–40 min, final full run ~90 min.
Parallel fixers per cluster are what make one round enough.

## Slices

Each lands on main; the machinery runs from `origin/prod`, so it takes
effect from the release after the one that deploys it. Harness files
(`scripts/round`, `scripts/release`, `scripts/ship`, `scripts/lib`) ship
`--remote` and are built by hand (builder deny paths).

### T1: fixes land on the release branch, one per cluster

- `precis.plans.templates.mint_release_fix`: the fix node's brief and
  task name `release/r<N>` as base and land target; the recut node goes.
- `ci_red_digest`: clusters failures and mints one fix node per cluster.
- `land_slice` (and `scripts/ship`'s land target, hand-built `--remote`):
  land onto a named release branch, rebasing on it, not main.
- Done when: a red gate with failures in two files mints two fix todos
  whose lands are commits on `release/r<N>`, and main does not move.

### T2: targeted re-run, then full run

- A `release_rerun` node after each fix round: runs the failed ids plus
  the spot-selected surround on a slice host against the release head
  (`scripts/test <ids>`), and records pass/fail per id on the plan.
- Green → a full gate run on the release head (push or `gh workflow
  run` on the branch); red → next fix round with the remaining ids.
- Done when: a round that fixes all failures goes rerun → full run →
  deploy ask with no re-cut and no new `release/r<N+1>`.

### T3: merge back with a real merge

- A `release_merge_back` node fired by the deploy start: `git merge
  --no-ff release/r<N>` into main on a slice host, push; hunks main
  already carries resolve by content; a real conflict mints one builder
  todo and holds the next cut until it lands.
- The next cut waits for the merge-back on main and `release_confirm`.
- Done when: after a deploy, main has one merge commit whose second
  parent is the release head, and `sha-in-prod` on a release-branch fix
  reads open.

### T4: reviews hold the deploy, scoped to the release

- `release_poll`'s review gate reads the cut's range plus the release
  branch's fix commits and holds the deploy node, not the cut.
- Retire the tip-review re-cut hold, `land_freeze` and their
  `PRECIS_CUT_FREEZE_*` settings, and `_RELEASE_LOOP_MAX`.
- Done when: lands after the cut, reviewed or not, never hold the
  release; an unreviewed land inside the range holds the deploy.

### T5: the alarm and immediate hold asks

- The no-progress / two-hour / not-code ask, with the main-commits-per-
  failing-test list.
- Hopeless holds file on the next poll (gr653037 a); deny-path routing
  only for paths the brief edits (gr653037 b).
- Done when: a round with no progress files one ask naming the stuck
  ids; a won't-do fix files its review-wait ask within one poll.

Order: T1 and T4 first (independent), then T2 (needs T1), T3, T5.

## Explicitly not in scope

- Faster full runs (shard split, gr576324 quick-guards): separate.
- Hotfix releases off a deployed release: not needed while every
  release is cut from main's tip.
- The manual deploy override (gr653038): Reto's decision, separate.
