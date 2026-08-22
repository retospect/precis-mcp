# Idle-but-live worktree test DBs hold VM RAM indefinitely

**Status:** open (deferred deliberately — Tier 2 of the 2026-08-21 sweep work)

`scripts/reap-test-dbs` (shipped `caaf8135`) closed the **orphan** case: a
`precis-test-*` compose project whose worktree no longer exists is now torn
down at SessionStart, whatever removed the tree. First run reaped 13 — trees
removed weeks earlier with their Postgres still resident, which is how routine
the event-coupled miss had become.

It deliberately does **not** touch the other population: projects whose
worktree still exists but has been untouched for days. On 2026-08-21 that was
`majestic-growing-zephyr` (24h) and `frolicking-kindling-platypus` (5 days,
though that one woke up mid-session and started a gate — see the trap below).
Each is a live postgres on the 8 GB colima VM.

## Why the orphan sweep can't just be widened

The orphan rule is a pure file test — "is the bind-mount source gone?" — which
is why it has no false positives and needs no liveness judgment. Idleness is
not a file test. Widening `reap-test-dbs` with a time heuristic would give a
safe, boring script a way to kill a DB out from under a parked session.

## Proposed shape

Separate lane, not a flag on the orphan sweep:

- Ask `scripts/inflight --json` for liveness — it is the repo's single source
  of truth for session/merge bucketing and must not be reimplemented (the same
  rule `reap-worktrees` follows).
- Reap only where there is **no live session** *and* no filesystem activity in
  the tree for ~7 days.
- Issue `stop`, **not** `down -v`. Reversible, volume retained, costs one
  container start. The orphan case earns `down -v` because nothing is coming
  back; this one does not.

## Traps

- **A quiet tree is not a dead tree.** `frolicking-kindling-platypus` showed a
  5-day-old DB and then started a gate 23 seconds into this session's check.
  Any time threshold must be generous, and `stop` must stay recoverable.
- **Reaping is not free.** The test DB persists across runs, so the next gate
  in that tree pays a full migration replay. Slow, not dangerous — and it
  happens to be the same reset that clears a migration-checksum mismatch.
- Related prior incidents where a reaper was too eager:
  `reap-live-worktree-incident.md`, `reaper-liveness-race.md`,
  `reaper-removed-live-session-worktree.md`. Read those before building this.

## Worth building?

Only if gate OOM-137 reds keep recurring. `scripts/lib/gate-slot.sh` already
caps concurrent gates at 2, which bounds the *active* memory spike; idle DBs
are a smaller, steadier tax. Measure before building — the orphan sweep may
have already reclaimed enough.
