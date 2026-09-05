# Worktree reaper raced a live session

A sibling SessionStart backstop reaped a live session's worktree right after
a ship (momentarily merged+clean, no purpose refresh; the liveness probe
missed the live session). Reap decisions should re-verify session liveness
immediately before `git worktree remove` and/or treat "session file active in
the last N minutes" as a hard veto. Repro window: ship → branch reset →
sibling session starts before the next local edit. Owner
`scripts/reap-worktrees` / `scripts/inflight`. Mechanical.

**Hardened 2026-09-05**: `scripts/reap-worktrees` re-verifies the full bucket
(lock/clean/merged) immediately before `git worktree remove`, after a shared
grace-period sleep (`PRECIS_REAP_GRACE_SECONDS`), and separately treats a
`.claude/purpose` file younger than `PRECIS_REAP_PURPOSE_FRESH_SECONDS` as a
hard veto — see docs/backlog/reaper-removed-live-session-worktree.md's
"Hardened 2026-09-05" section for the full writeup and the reasoning for why
`scripts/hooks/session-end-reap.sh` doesn't need the same treatment.
