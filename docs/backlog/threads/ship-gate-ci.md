---
roster: active
pillar: operations
effort: high
design-review: yes
---
# ship-gate-ci

## Resume

- **Next:** Verify Do next 1 (gr495187: both halves landed — the ship-side cap of 2 concurrent `ci/*` gates, then the slim main-push lane per Reto 2026-10-23; check the drift guard keeps reading release/nightly verdicts on the first pushes after it lands). The prod-as-second-parent check is covered by tests/test_round_release.py::test_release_deploy_second_parent_tag_merge_and_retirement (355212c02); release rounds are done.
- **Blocked by:** Nothing.
- **Unblocks:** Verified release deployments for every thread.
- **Acceptance:** Use the [release-cycle runbook](../../runbooks/release-cycle.md) and the verification steps in [Do next](#do-next); check current deployment and worktree state before acting.
- **Worktree:** `ship-gate-ci`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

A partial release stays pinned; if its 6h verdict expires, re-gate the same
head through `scripts/release` before retrying `scripts/round deploy`. Do not
abandon a partial journal.

Current declared activity: the `roster:` front matter above; dated allocation decisions below are historical.

**Status:** ends when the ship gate, CI lanes and worktree reaper never
hold the fleet hostage, never read a policy crash as a violation, and never
delete a live session's tree — the path every pillar's work ships through
(`docs/roadmap.md` Pillar 5 Operations). Today twenty filed items have no owner;
the order is data loss first (live-worktree deletion), then fleet-wide
stalls (slot and lock holds, hangs), then verdict honesty (red that reads as
green or the reverse), then tuning and residue.
**Last reviewed:** 2026-10-17
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

2026-10-15 ct-gate: rebased past `--checked` (72242c37a) and cut to the
ruling "keep the full suite on releases for now" — impacted-only release
gating is DEFERRED until the selector has a track record (Reto ~09:00Z).
What survives: `scripts/impacted-tests` is the ONE selector — it absorbed
select-tests' CONTRACT/PATH_MAP/corpus/import tiers (same `testmon=N` wire
protocol; `scripts/select-tests` deleted). `--quick` stays lint-only;
`--checked` is lint + the selector's picked tests (FULL refuses; use
`--quick`/`--full`/`--remote`); `--no-tests` is gone. `scripts/test
--selected` previews it. The nightly `coverage-db` job feeds the measured
tier. Commits: 35b60f9a2 selector+smoke, ce99655d7 --checked, 1101850c3
coverage DB. Deferred slice, designed + measured: `test-release` CI lane
(lint + impacted ∪ smoke ∪ migrations over `deployed/r<N-1>`, `test-linux`
as non-blocking soak, verdict via `last-gated-main-sha --lane release`) —
the reverted commits show the wiring (8e94e8c33-era, pre-drop sha).
~15–30 min static-only on release diffs; tighter once the measured tier
feeds it. First real `--checked` ship is the dogfood; watch for the
`coverage-db` job's artifact + selector drift on missing evidence.
Left: gripe-filing from a red soak leg is deliberately out of scope
(job summary only); `release-ci-diff-coverage.md` now has its DB produced
nightly — the diff-cover verdict is still open.

## Do next

0. **[`release-train.md`](../release-train.md)** (draft, Reto 2026-10-23
   ruling) — qland everything, harness-only `--remote`, one 10-shard
   release gate fixed in place, next cut at deploy start. 7 slices; two
   questions open for Reto at its end. Supersedes the "one release at a
   time" model.
1. **gr495187** — CI runner starvation: main runs waited ~70 min for shard
   runners. Measured twice (gripe comments 1 and 4): mean demand is only
   ~12.4 of 40 runners; the failure was unbounded `ci/*` gate fan-out (peak
   20 concurrent gates, 77% of at-cap jobs, 10–14% of the week at cap)
   stacked on every main push running the full matrix (~32% of
   job-minutes). Two fixes shipped: `scripts/ship --remote` admits at most
   `PRECIS_REMOTE_GATE_SLOTS` (2) queued+in-flight `ci/*` runs before
   pushing its gate ref, and the **slim main lane** (Reto 2026-10-23:
   "qland all, then one release ... only release branch gets ci") — a push
   to main is now plan + lint (+ the docs lane for docs-only deltas), the
   matrix is reserved for `ci/**`, `release/**`, the nightly and `full`
   dispatches. A main sha's verdict comes from the release cut on it or
   the nightly; `scripts/last-gated-main-sha` drops CANCELLED check-runs
   so a displaced slim run can't veto those. No staging branch: under
   qland main already is staging, `release/r<N>` is the tested cut. Left:
   verify the drift guard reads release/nightly verdicts and a qland burst
   costs ~2 jobs/push; the impact-selected main lane from gripe comment 5
   stays a follow-up if lint proves too thin.
2. **outbound-policy S3** — SHIPPED 2026-10-23 (spec deleted; the gates
   live in `invariants.md` §Outbound): `tests/test_outbound_timeout_ratchet.py` +
   `tests/data/outbound_timeout_baseline.json` (frozen at 100 sites/88
   keys, one-way floor; `while True` polls and tests/ report-only),
   `tests/test_outbound_polite_ratchet.py` tightened per call, ruff S113
   on for src/+scripts/. It absorbed gr503084's parts B and C; gr503084
   is closed and its part A (CI hang debuggability) went with p0-ci.

3. **release-candidate-verdicts** (item deleted 2026-10-12, all settled) — p1 (Reto 2026-10-03). Main's
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
   `range=f953a5fae..HEAD`). Queue delay (run created → first job
   started, 150 sampled runs per week, measured 2026-10-12): `ci/**` ship
   gates were unaffected, median 0.05 min / p90 0.07 (09-26..10-03) vs
   0.05 / 0.10 (10-04..10-11). Main runs now wait in the shared group:
   median 0.63 → 1.72 min, p90 1.43 → 18.4; 77 of 150 main runs never
   started (a pending run replaced by a newer push, as designed). Ruled
   2026-10-12 (Reto): the max candidate age stays 6 h
   (`DEFAULT_MAX_CANDIDATE_HOURS` in `scripts/round`); nothing open.
4. **backlog/reaper-removed-live-session-worktree.md** — auto-reap deleted
   live sessions' trees; fixes 1–3, the grace/purpose guards and the
   two-sessions-in-one-tree process-table check shipped,
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
5. **backlog/orphaned-test-runs-hold-gate-slots-forever.md** — a subagent
   exiting without reaping `scripts/test` holds a slot forever, starving the
   slot-capped gate for every tree.
6. **backlog/gate-hang-diagnosis.md** — py-spy cannot run inside the gate
   container; the tooling that makes 5 diagnosable.
7. **backlog/local-gate-red-on-green-main-token-budget.md** — gating CI is
   3.13-only but prod runs 3.12; nightly red on a green main.


## Horizon

3. **backlog/gate-concurrency.md** — serialised template clones add
   suite-setup tax; speed, not correctness.
4. **backlog/idle-test-dbs-hold-vm-ram.md** — live worktree test DBs hold VM
   RAM; Tier 2 reaping deferred.
5. **backlog/per-agent-green-is-not-integrated-green.md** — six subagents
   each green, the integrated run red; a workflow gap, not a defect.
6. **backlog/ops-gate-hygiene.md** — service_config rollback gates need
   expiry and review; a housekeeping grab-bag.
7. **backlog/worktree-path-guard-false-positives.md** — the brief tells
   agents to read other worktrees; the harness refuses.
8. **backlog/ruff-is-unpinned-across-worktrees.md** — a `ruff>=0.11` floor
   lets trees format differently, so qland lint drifts.
9. **backlog/piped-exit-guard-tuning.md** — guard-piped-exit-code
    false-positive rate vs value; a decision, not a defect.
10. **backlog/token-review-hook-gaps.md** — the bash-reflex nudge misses
    real traffic; compact-thrash re-reads.
11. **backlog/fleet-coordination-via-precis.md** — peer messages and
    wakeups cost a full ~250k-context turn each; consider moving the
    status relay into precis.

## Parked

- (none)

## No action needed

## Seam

`monitors-that-go-quiet` owns signals that lie; this thread owns the gate,
the reaper and the qland drift guard that feeds that thread's "is main
green" answer. `deploy-fleet-ops` owns the
rest of `deploy-async-task-controller-filenotfounderror.md` (whether a
vanished play file should abort a running play; the apt stall).

`backlog/backlog-lint-flags-ticked-subitems-as-shipped.md` was named by the
platform pass but has no file; file it before ranking it here.
