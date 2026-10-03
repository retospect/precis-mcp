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
   holds the ship lock. Ship the hygiene-tests-into-pre-qland-lint slice
   first and alone, then no-cancel on main, the time-bounded
   `last-gated-main-sha`, and `scripts/round gate|deploy` on the newest
   green main sha. Review item organizer-release-branch-1 (v2) has the
   critique and Reto's two open decisions; design-bearing changes go to the
   orchestrator as a design note before they land.
2. **backlog/fleet-orchestrator-verbs.md** — the orchestrator's repeated
   hand sequences as `scripts/fleet` verbs (verdict, say -m/--when-clear,
   peek/dialogs, compact --at-idle, mcp-check, one watcher, refs). Mined
   from its transcript 2026-10-03; builds after 1.
3. **backlog/reap-live-worktree-incident.md** — auto-reap deleted a live
   session's tree twice, killing in-flight prod runs; data loss outranks
   every stall below. Overlaps 2: settle which one survives before fixing.
4. **backlog/reaper-removed-live-session-worktree.md** — the same
   lock-silently-released failure, fixes 1–3 shipped, harness-event root
   cause open; read with 1.
5. **backlog/inflight-lists-the-live-deploy-render-tree-as-removable.md** —
   inflight tells agents to delete the tree a deploy reads; same blast
   class as 1.
6. **backlog/orphaned-test-runs-hold-gate-slots-forever.md** — a subagent
   exiting without reaping `scripts/test` holds a slot forever, starving the
   2-slot gate for every tree.
7. **backlog/local-gate-holds-the-ship-lock-for-its-whole-run.md** — a local
   gate serialises the fleet for up to 1h43m; 4's sibling on the lock.
8. **backlog/gate-hang-diagnosis.md** — py-spy cannot run inside the gate
   container; the tooling that makes 4 and 5 diagnosable.
9. **backlog/policy-gates-must-fail-distinguishably.md** — a secret-scan
   crash reads as a policy violation, sending authors to fix the wrong thing.
10. **backlog/local-gate-red-on-green-main-token-budget.md** — gating CI is
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
  delete after a week of real bursts (the file's own rule); deletion is
  Reto's housekeeping ruling (td461205).

## Seam

`plugin-split` owns the plugin-boundary and image work that
`pathway-plugin-ci-image` waits on. `monitors-that-go-quiet` owns signals
that lie; this thread owns the gate and reaper, and `main-stays-gated`
feeds that thread's "is main green" answer. `deploy-fleet-ops` shares
`scripts/deploy`'s render tree with item 3.

`backlog/backlog-lint-flags-ticked-subitems-as-shipped.md` was named by the
platform pass but has no file; file it before ranking it here.
