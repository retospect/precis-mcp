---
status: idea
pillar: platform
---

# Reaper removed a live session's worktree after its lock was silently released

Compacted 2026-10-07 (status review); the full incident record (2026-08-15
`fluttering-spinning-blanket`, recurrences 08-15 and 08-25) is in git
history of this file. Kept because `scripts/reap-worktrees`,
`scripts/hooks/session-end-reap.sh`, `scripts/lib/session-lock.sh`,
`scripts/ship` and `tests/test_worktree_lock_reap.py` cite the proposals
by number.

**What happened.** A live session's `git worktree lock` was released
without the session ending; after its ship left the tree clean and
merged, a sibling's SessionStart reaper bucketed it `safe_remove` and
deleted 1,337 tracked files. Fully recovered from the shipped commit.
Proven mechanism (08-25): a nested `claude` subprocess fires SessionEnd
semantics for the parent session.

**Hardening proposals and their state.**

1. Grace period in `safe_remove` — SHIPPED 2026-09-05
   (`PRECIS_REAP_GRACE_SECONDS`, re-bucket before remove).
2. `.claude/purpose` as a tripwire — SHIPPED 2026-09-05
   (`PRECIS_REAP_PURPOSE_FRESH_SECONDS`, checked before and after the sleep).
3. Ship re-asserts the session lock — SHIPPED 2026-08-27
   (`reassert_session_lock`, `scripts/lib/session-lock.sh`).
   Root cause closed by `_NESTED_SESSION_ENV` stamping `PRECIS_NO_AUTOREAP=1`
   in `utils/_claude_subprocess.py`; `session-end-reap.sh` only reaps a tree
   whose lock the ending session provably held.
4. Harness kill / SessionEnd coupling — OPEN. The 08-15 trigger (three
   background tasks killed at once, lock dropped, tracked files partly
   deleted while ignored files survived) was never reproduced; 1–3 make it
   harmless, not understood.

**Still true.** A lockless + clean + merged tree is removable by any
sibling's SessionStart; the lock is a liveness proof with one point of
failure. Reopen as a spec only if a third event lands from a window
proposals 1–3 do not cover.

test: `scripts/test tests/test_worktree_lock_reap.py` stays green; a
tree with a purpose file younger than 6 h is skipped by `scripts/reap-worktrees`.
