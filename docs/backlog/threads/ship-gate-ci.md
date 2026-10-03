# ship-gate-ci

**Status:** ends when the ship gate, CI lanes and worktree reaper never
hold the fleet hostage, never read a policy crash as a violation, and never
delete a live session's tree — the path every pillar's work ships through
(`docs/roadmap.md` platform bucket). Today twenty filed items have no owner;
the order is data loss first (live-worktree deletion), then fleet-wide
stalls (slot and lock holds, hangs), then verdict honesty (red that reads as
green or the reverse), then tuning and residue.
**Last reviewed:** 2026-10-03
**Worktree:** `ship-gate-ci`
**Active:** yes — Reto 2026-10-03 ("push should not break build"; set off as p1).

## Do next

1. **backlog/release-candidate-verdicts.md** — p1 (Reto 2026-10-03). Main's
   CI runs cancel each other at ~8 qlands/hour, so main has no verdicts and
   the drift guard is blind (25-commit walk); the round's local full gate
   holds the ship lock. All four slices shipped 2026-10-03 (hygiene tests
   in the pre-qland lint; un-cancelled main runs (per-sha, replaced by a
   shared never-cancel-running group 2026-10-03, Reto, review-queue
   ship-gate-ci-1, after main queued 18 runs at ~33 pushes/h); 48 h GraphQL
   walk with a looked-none exit 2; `scripts/round gate|deploy`; design note
   + verdict in `reviews/ship-gate-ci*.md`). Acceptance met 2026-10-03:
   all six main pushes 12:16–12:40Z completed, none cancelled (four inside
   5 min); f953a5fae was the first green candidate. Prod dogfood 13:55Z:
   round 2 deployed 63301c5c, whose verdict was fully green (lint + 6
   shards), and `gated` = `prod` = 63301c5c. The next main push had a green
   verdict 0.2 h after it landed, and `plan` resolved the last green sha
   over GraphQL under `checks: read` (run 37125755551:
   `range=f953a5fae..HEAD`). Open: (b) one week of
   `ci/**` queue delay (created → first job started) before vs after,
   reported here; (c) Reto's max-candidate-age ruling changes only
   `DEFAULT_MAX_CANDIDATE_HOURS`. Baseline: 0 of main's 52 commits in the
   12 h to 12:00Z had a green verdict. Review item organizer-release-branch-1 (v2) has the
   critique and Reto's two open decisions; design-bearing changes go to the
   orchestrator as a design note before they land.
2. **backlog/release-branch-rounds.md**: Reto 2026-10-03, ship-gate-ci-1.
   Each round cuts `release/r<N>`, fixes land on it and merge forward into
   main, and the deploy tags `deployed/r<N>` and merges back. Design note 3
   and its verdict (build it) are in `reviews/ship-gate-ci*.md`. Slice (a)
   (`round cut`, `release/**` CI) shipped 06247b684. Next is (b) `ship
   --release`, which needs its own note first and Reto's forward-merge
   answer (review-queue ship-gate-ci-1); then (c).
3. **backlog/reaper-removed-live-session-worktree.md** — auto-reap deleted
   live sessions' trees; fixes 1–3 and the grace/purpose guards shipped,
   the harness kill/SessionEnd coupling (proposal 4) is open. Its sibling
   incident file closed 2026-10-03: the ownership guard (e3135337c) fixed
   the spurious-SessionEnd unlock and `scripts/inflight` now buckets a
   non-`pid` lock `needs_judgment`. The deploy render-tree item closed the
   same day: the tree is pid-suffixed and pid-locked (2026-09-29), every
   tree-deleting liveness check (deploy lock steal and sweep, inflight,
   session-end-reap) reads EPERM as alive, and a vanished tree is named
   when ansible fails. The agent test-db item closed the same day too:
   `scripts/reap-test-dbs` downs an `agent-*` tree's idle db after 2 h
   (was 48 h), and `scripts/test` names the reaper on docker's "fully
   subnetted" error. The 2 h reap alone did not stop it: the pools ran
   out again at ~18:25Z. So `scripts/test` now tears an `agent-*` tree's
   project down at exit (ae5084ae3), and colima's docker pools were widened
   from ~31 to 256 networks (Reto, review-queue ship-gate-ci-2, done by
   the orchestrator 19:01Z).
4. **backlog/deploy-renders-only-precis-roles.md** — `scripts/deploy` never
   renders backups, monitoring or pgbouncer roles; a B2 sync fix sat
   unrendered for 7 weeks. Draft, Reto picks (i)/(ii)/(iii).
5. **backlog/orphaned-test-runs-hold-gate-slots-forever.md** — a subagent
   exiting without reaping `scripts/test` holds a slot forever, starving the
   2-slot gate for every tree.
6. **backlog/local-gate-holds-the-ship-lock-for-its-whole-run.md** — a local
   gate serialises the fleet for up to 1h43m; the round no longer runs one
   (2026-10-03), `/go` still does.
7. **backlog/gate-hang-diagnosis.md** — py-spy cannot run inside the gate
   container; the tooling that makes 5 and 6 diagnosable.
8. **backlog/policy-gates-must-fail-distinguishably.md** — a secret-scan
   crash reads as a policy violation, sending authors to fix the wrong thing.
9. **backlog/local-gate-red-on-green-main-token-budget.md** — gating CI is
   3.13-only but prod runs 3.12; nightly red on a green main.

## Horizon

1. **backlog/test-db-seed-xdist-isolation.md** — shared test-DB seeds vanish
   on gate clones; isolation fixes remove a flake class.
2. **backlog/gate-concurrency.md** — serialised template clones add
   suite-setup tax; speed, not correctness.
3. **backlog/idle-test-dbs-hold-vm-ram.md** — live worktree test DBs hold VM
   RAM; Tier 2 reaping deferred.
4. **backlog/per-agent-green-is-not-integrated-green.md** — six subagents
   each green, the integrated run red; a workflow gap, not a defect.
5. **backlog/pathway-plugin-ci-image.md** — the pathway plugin is untested
   until the dev image carries autocatpath; couples to plugin-split.
6. **backlog/ops-gate-hygiene.md** — service_config rollback gates need
   expiry and review; a housekeeping grab-bag.
7. **backlog/worktree-path-guard-false-positives.md** — the brief tells
   agents to read other worktrees; the harness refuses.
8. **backlog/ruff-is-unpinned-across-worktrees.md** — a `ruff>=0.11` floor
   lets trees format differently, so qland lint drifts.
9. **backlog/windows-ci-residuals.md** — Windows timing-flake watch after
   the skipif pass.
10. **backlog/piped-exit-guard-tuning.md** — guard-piped-exit-code
    false-positive rate vs value; a decision, not a defect.
11. **backlog/token-review-hook-gaps.md** — the bash-reflex nudge misses
    real traffic; compact-thrash re-reads.

## Parked

- (none)

## No action needed

- **backlog/main-stays-gated.md** — all three parts shipped 2026-09-30;
  Reto ruled (review-queue `organizer-housekeeping-1`): delete it plus its
  seam mentions (threads/INDEX.md, this file's Seam) in one commit on or
  after 2026-10-07; git history is the backup.

## Seam

`plugin-split` owns the plugin-boundary and image work that
`pathway-plugin-ci-image` waits on. `monitors-that-go-quiet` owns signals
that lie; this thread owns the gate and reaper, and `main-stays-gated`
feeds that thread's "is main green" answer. `deploy-fleet-ops` owns the
rest of `deploy-async-task-controller-filenotfounderror.md` (whether a
vanished play file should abort a running play; the apt stall).

`backlog/backlog-lint-flags-ticked-subitems-as-shipped.md` was named by the
platform pass but has no file; file it before ranking it here.
