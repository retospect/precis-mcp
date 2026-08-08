# Worktree reaper raced a live session

A sibling SessionStart backstop reaped a live session's worktree right after
a ship (momentarily merged+clean, no purpose refresh; the liveness probe
missed the live session). Reap decisions should re-verify session liveness
immediately before `git worktree remove` and/or treat "session file active in
the last N minutes" as a hard veto. Repro window: ship → branch reset →
sibling session starts before the next local edit. Owner
`scripts/reap-worktrees` / `scripts/inflight`. Mechanical.
