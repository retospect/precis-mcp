# The mkdir-mutexes need a liveness heartbeat, not just a pid check

Follow-up to the reclaim-rule fix (`scripts/lib/lock-holder.sh`, shipped
2026-09-14). That change stopped the age timer from stealing a lock out from
under a **live** holder — the bug that let two ships run concurrently. It
left a gap in the opposite direction, and this item closes it.

## The gap

`lock_holder_reclaim_reason` now refuses to reclaim any holder whose pid is
alive on this host, *however long it has held*. That is right for a slow
holder (`ship --mutate` plus a full gate runs 30-90 min) and wrong for a
**hung** one: a ship wedged on a stuck container, an unresponsive mount, or a
`--remote` CI wait that never returns holds the lock forever, and every
sibling ship queues behind it indefinitely.

The old 30-minute steal accidentally covered this case. It was the wrong
mechanism — it could not distinguish hung from slow, so it broke healthy
ships — but it did guarantee the fleet eventually moved. Removing it without
a liveness signal trades "concurrent ships on a slow holder" for "fleet-wide
block on a hung holder".

Severity is moderate rather than high: the block is loud (every waiting
sibling prints `waiting for the ship lock — held by: <worktree> pid=<n>`), so
it is diagnosable in one look and clears with a kill. But it needs a human,
which the previous behaviour did not.

## Fix

Make the age timer measure **lack of progress** instead of hold duration, so
it can apply to a live local holder without punishing a slow one:

1. `touch "$LOCKDIR"` at each phase boundary in `scripts/ship` (sync, gate
   start, each gate stage, squash) and from `gate_slot_acquire`'s holder while
   its gate runs. A cheap `lock_holder_beat <dir>` in `lock-holder.sh` keeps
   this one line at each call site.
2. Extend `lock_holder_reclaim_reason`: a live local holder becomes
   reclaimable when its lock has not been beaten for N minutes — i.e. the
   process exists but has made no progress. Pick N comfortably above the
   longest single phase (the full gate's pytest stage is the long pole at
   ~30 min, so N=45 for the ship lock is not obviously right — measure the
   worst-case gap between beats before choosing).
3. Keep the existing paths unchanged: dead local pid steals at once, foreign
   and unparseable holders steal on plain age.

The beat interval is the whole design risk. Too coarse and a healthy ship
looks hung; the safe direction is to beat *more* often than feels necessary,
including from inside the gate's own progress output, so N can be small
without false positives.

## Test

Extend `tests/test_lock_holder_reclaim.py`, which already sources the lib
directly:

- live local holder, lock beaten just now, aged mtime irrelevant → NOT
  reclaimable (the existing regression case, still protected);
- live local holder, no beat for N+ minutes → reclaimable, reason names the
  stall rather than the hold;
- `lock_holder_beat` refreshes mtime without touching the holder file's pid or
  host fields (a beat must not make a lock look like someone else's).
