# Reaper removed a live session's worktree after its lock was silently released

**Severity: data-loss near-miss** (fully recovered — everything reachable
from the shipped commit; only gitignored `.claude/purpose` was lost).

## What happened (2026-08-15, worktree `fluttering-spinning-blanket`)

1. A harness-side event killed three background tasks of a **live, continuing**
   session at once (a ship run + two waiters). The session's `git worktree
   lock` was **released** in the same event — the recorded pid (63458) stayed
   alive throughout, so this was an unlock (SessionEnd semantics firing for a
   session that was not actually ending), not a dead pid.
2. The session re-ran `scripts/ship`, which landed `de0bcb65` and — by
   design — left the worktree **clean** and reset to shipped main.
3. Within minutes, 1,337 tracked files were deleted from the worktree
   (`tests/` entirely gone), while gitignored content (`.venv/`,
   `__pycache__/`) survived and the worktree stayed **registered** in
   `git worktree list`. `scripts/inflight` at that point showed the tree
   with **no lock** (`—`); a lockless + merged + clean tree buckets
   `safe_remove`, which both `scripts/reap-worktrees` (sibling SessionStart)
   and `scripts/hooks/session-end-reap.sh` treat as removable.
4. Recovery: `git ls-files -d -z | xargs -0 git checkout --` restored all
   tracked files; in-flight uncommitted work (one modified file) was
   untouched; lock re-acquired via `scripts/hooks/session-start-lock.sh`
   (re-locked to the same, still-alive pid — proving the pid never died).

## Open questions

- **Partial deletion mechanism**: `git worktree remove` should either refuse
  (untracked files present) or delete the whole directory (`.venv` included)
  and deregister. Observed state matches neither cleanly — tracked files
  gone, ignored files intact, worktree still registered. Something
  interrupted mid-removal, or the remover was not `git worktree remove`.
- **Why the lock released**: the session continued after the kill event, so
  either the harness fired SessionEnd for a live session, or something else
  ran `git worktree unlock`. The reaper itself unconditionally unlocks trees
  it has already bucketed `safe_remove` — but bucketing happens *before*
  that unlock, so the lock must have been gone (or its pid unreadable) at
  bucketing time.

## Recurrence (same session, ~1 h later)

The kill event **repeated**: another batch of live background tasks
(including a mid-gate `scripts/ship` run) was killed at once, and the
worktree lock was released again — re-verified gone via
`git worktree list --porcelain`, re-acquired with the same still-alive pid
via `scripts/hooks/session-start-lock.sh`. Two-for-two: every kill batch
released the lock of a session that kept running. No reap followed this
time only because the tree was dirty (mid-gate ruff amendments). This
upgrades proposal 4 from "investigate" to "the root cause": whatever
harness event kills background tasks (plausibly context compaction) also
runs SessionEnd-like teardown including the lock release.

## Hardening proposals

1. **Grace period in `safe_remove`**: require the tree to have been
   continuously lockless+clean for N minutes (e.g. re-check after a 60 s
   sleep) before removal — a just-shipped tree whose session is mid-turn is
   exactly the false positive observed.
2. **`.claude/purpose` as a tripwire**: a fresh (< a few hours) purpose file
   should demote `safe_remove` → `needs_judgment`; sessions write it at task
   start, and it is deleted with a clean SessionEnd.
3. **Re-lock after ship**: `scripts/ship` could re-assert the session lock as
   its final step (it runs inside the live session), closing the
   lockless+clean window it otherwise opens.
4. Investigate the harness kill event → SessionEnd-hook coupling: killing
   background tasks must not release the worktree lock of a continuing
   session.
