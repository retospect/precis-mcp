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

## Recurrence 2026-08-25 (`zesty-painting-volcano`) — and a proven mechanism

Same signature, larger blast: 1497 tracked files deleted out from under a
live session; gitignored content (`.venv/`, `__pycache__/`) intact; the
worktree still registered in `git worktree list`; `git restore .` recovered
everything only because the branch was already merged. Filed as gripe 256469.

This run had a trigger we could actually pin down, and it answers two of the
open questions above.

**"Why the lock released."** A nested `claude -p` — spawned by an ordinary
big-tier LLM call (`taproot repair-evidence --tier big`) — inherits the
caller's cwd and the project's hook wiring, so it runs the full
SessionStart/SessionEnd lifecycle against the *caller's* worktree. Two
compounding bugs then zero the lock:

1. `session-start-lock.sh::find_session_pid` walks to the nearest `claude`
   ancestor, which for the nested hook is the nested process itself — so it
   unlock-then-relocks the tree to the nested pid, clobbering the real
   session's.
2. The nested one-shot exits immediately; `session-end-reap.sh` releases the
   lock **unconditionally, before the bucket check**, with no test of whose
   lock it is. The lock is now simply gone.

`scripts/inflight::session_field` reads liveness *only* from that lock
reason, so the tree reports `—` while its session is plainly still running,
and a clean+merged tree then buckets `safe_remove`.

Whether this is also what happened on 08-15 is unproven — that session
attributed the release to a harness kill/compaction event. But the observed
end states are identical down to the odd details, so proposal 4 above should
be re-read with this mechanism in hand before assuming a second, distinct
harness-side cause.

**"Partial deletion mechanism."** Consistent with `git worktree remove`
beginning removal and aborting when it reached the untracked/ignored files
it refuses to delete — which leaves precisely tracked-gone / ignored-intact /
still-registered. Not confirmed, but it no longer looks like "neither".

### What has been fixed

The primary cause is closed at the source: `_claude_subprocess.run_claude` /
`run_claude_async` now stamp `PRECIS_NO_AUTOREAP=1` onto every nested
subprocess env, at the same chokepoint the OAuth bootstrap uses. All three
reapers honour that flag, so a nested model call can no longer reap at
SessionEnd, reap via the SessionStart backstop, or release the real session's
lock. Hardening of the two hook bugs themselves is tracked in gripe 256469.

### What is still open — and it is the item below, not that fix

Both fixes above only stop *nested `claude`* from zeroing the lock. The
exposure this document was opened for is untouched: **any** worktree that is
lockless + clean + merged is removable by the next `SessionStart` of **any
sibling session**, with no check that a live session is sitting in it. The
lock is a single point of failure being used as a liveness proof, and today
showed it can be dropped by something as ordinary as an LLM call. Proposals
1-3 above (grace period / `.claude/purpose` tripwire / re-lock after ship)
remain the live design question, and today raises their priority: this is now
two data-loss events in ten days from the same weak signal.
