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

5. Two sessions in one tree (third event, gr474985, 2026-10-08: 2,546
   files) — SHIPPED. A second `claude` started by hand in a held tree took
   the lock; its SessionEnd then reaped the tree under the first. Now
   `session-start-lock.sh` never takes a live lock, and `session-end-reap.sh`
   plus `scripts/reap-worktrees` ask the process table (`other_session_in`)
   for another session whose cwd is in the tree, and relock to it instead
   of removing.

**Recovery** from a part-removed tree: `git restore --source=HEAD -- .`,
then move the emptied `.venv` aside and `uv sync` (git cannot restore it).

**Still true.** The lock records one pid; the process-table check covers
sessions on this host only. Reopen as a spec if another event lands from a
window proposals 1–5 do not cover.

test: `scripts/test tests/test_worktree_lock_reap.py` stays green; a
tree with a purpose file younger than 6 h is skipped by `scripts/reap-worktrees`.
