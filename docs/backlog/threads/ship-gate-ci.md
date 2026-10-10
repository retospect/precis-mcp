# ship-gate-ci

## Resume

- **Pillar:** platform
- **Next:** Reap idle gate/test-db pairs (docker-vm-disk-fills-silently, Do next 2); slices (a)–(c) of the release-branch-rounds item are all on main (item deleted 2026-10-07) (`scripts/round` tags `deployed/r<N>` and merges back; tags r6–r16 exist on origin).
- **Blocked by:** Nothing; verify the prod-as-second-parent test exists before retiring Do next 1.
- **Unblocks:** Verified release deployments for every thread.
- **Acceptance:** Use the [release-cycle runbook](../../runbooks/release-cycle.md) and the verification steps in [Do next](#do-next); check current deployment and worktree state before acting.
- **Worktree:** `ship-gate-ci`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: [fleet roster](../../../.claude/fleet/threads.tsv); dated allocation decisions below are historical.

**Status:** ends when the ship gate, CI lanes and worktree reaper never
hold the fleet hostage, never read a policy crash as a violation, and never
delete a live session's tree — the path every pillar's work ships through
(`docs/roadmap.md` platform bucket). Today twenty filed items have no owner;
the order is data loss first (live-worktree deletion), then fleet-wide
stalls (slot and lock holds, hangs), then verdict honesty (red that reads as
green or the reverse), then tuning and residue.
**Last reviewed:** 2026-10-04
**Worktree:** `ship-gate-ci`
**Allocation decision (historical):** yes — Reto 2026-10-03 ("push should not break build"; set off as p1).
**Resume:** 2026-10-04 07:30Z. Everything this thread built is on main;
the tree holds no unlanded work. Live in prod since round 4 (727728cc9):
the narrow ship lock (forward-merge land with a `Gate:` trailer after the
race budget), the docker-disk preflight and build-cache cap, `fleet say`
bracketed paste, agent-tree teardown run markers. Round 6, marked in,
awaiting the deploy: 13bac2b25 (release slice (b), `ship --release`),
40bd57ff1 (remote race budget by risk: one CI run, then forward-merge;
one retry when the tree adds a migration or touches `safe_fetch.py`),
68e65b30a (`scripts/fleet up <slug…>` creates only the named windows).
40bd57ff1's own ship dogfooded the budget: main moved during its one CI
run and it landed forward-merged over 7 commits, no pin, `gated` not
moved. Not yet seen running: the build-cache cap (cache was under 30 GB)
and `ship --release` on a real round. Open: Do next 1 slice (c), then 2.
Next step: slice (c), dogfooded on the first round cut after it lands.

## Do next

1. **Release rounds (was backlog/release-branch-rounds.md, deleted 2026-10-07 as
   shipped)**: slice (a) `round cut` + `release/**` CI (06247b684), slice (b)
   `ship --release` (2026-10-04), slice (c) `round gate`/`deploy` on the
   release head with the `deployed/r<N>` tag and merge-back (`scripts/round`;
   tags `deployed/r6`–`r16` on origin). Left here: confirm the
   prod-as-second-parent test exists under `tests/`; if not, add it.
2. **backlog/docker-vm-disk-fills-silently.md** — a 90 GB build cache filled
   the VM disk on 2026-10-03, and every gate died on a raw ENOSPC.
   The `scripts/test` and `scripts/ship` refusals below 10 GB and the
   reaper's cache cap shipped; left: reaping idle gate/test-db pairs.
3. **backlog/release-candidate-verdicts.md** — p1 (Reto 2026-10-03). Main's
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
4. **backlog/reaper-removed-live-session-worktree.md** — auto-reap deleted
   live sessions' trees; fixes 1–3, the grace/purpose guards and the
   two-sessions-in-one-tree process-table check (gr474985) shipped,
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
5. **backlog/deploy-renders-only-precis-roles.md** — `scripts/deploy` never
   renders backups, monitoring or pgbouncer roles; a B2 sync fix sat
   unrendered for 7 weeks. Draft, Reto picks (i)/(ii)/(iii).
6. **backlog/orphaned-test-runs-hold-gate-slots-forever.md** — a subagent
   exiting without reaping `scripts/test` holds a slot forever, starving the
   2-slot gate for every tree.
7. **backlog/gate-hang-diagnosis.md** — py-spy cannot run inside the gate
   container; the tooling that makes 6 diagnosable.
8. **backlog/local-gate-red-on-green-main-token-budget.md** — gating CI is
   3.13-only but prod runs 3.12; nightly red on a green main.

## Horizon

1. **backlog/session-prefix-and-fleet-context-cost.md** — a 105-137k
   turn-one prefix is half of all cache reads; fleet windows run at 250k.
2. **backlog/test-db-seed-xdist-isolation.md** — shared test-DB seeds vanish
   on gate clones; isolation fixes remove a flake class.
3. **backlog/gate-concurrency.md** — serialised template clones add
   suite-setup tax; speed, not correctness.
4. **backlog/idle-test-dbs-hold-vm-ram.md** — live worktree test DBs hold VM
   RAM; Tier 2 reaping deferred.
5. **backlog/per-agent-green-is-not-integrated-green.md** — six subagents
   each green, the integrated run red; a workflow gap, not a defect.
6. **backlog/pathway-plugin-ci-image.md** — the pathway plugin is untested
   until the dev image carries autocatpath; couples to plugin-split.
7. **backlog/ops-gate-hygiene.md** — service_config rollback gates need
   expiry and review; a housekeeping grab-bag.
8. **backlog/worktree-path-guard-false-positives.md** — the brief tells
   agents to read other worktrees; the harness refuses.
9. **backlog/ruff-is-unpinned-across-worktrees.md** — a `ruff>=0.11` floor
   lets trees format differently, so qland lint drifts.
10. **backlog/windows-ci-residuals.md** — Windows timing-flake watch after
    the skipif pass.
11. **backlog/piped-exit-guard-tuning.md** — guard-piped-exit-code
    false-positive rate vs value; a decision, not a defect.
12. **backlog/token-review-hook-gaps.md** — the bash-reflex nudge misses
    real traffic; compact-thrash re-reads.
13. **backlog/fleet-coordination-via-precis.md** — peer messages and
    wakeups cost a full ~250k-context turn each; consider moving the
    status relay into precis.

## Parked

- (none)

## No action needed

## Seam

`plugin-split` owns the plugin-boundary and image work that
`pathway-plugin-ci-image` waits on. `monitors-that-go-quiet` owns signals
that lie; this thread owns the gate, the reaper and the qland drift guard
that feeds that thread's "is main green" answer. `deploy-fleet-ops` owns the
rest of `deploy-async-task-controller-filenotfounderror.md` (whether a
vanished play file should abort a running play; the apt stall).

`backlog/backlog-lint-flags-ticked-subitems-as-shipped.md` was named by the
platform pass but has no file; file it before ranking it here.
