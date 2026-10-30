---
description: "Review all worktrees/branches in flight and clean up the safe ones; ambiguous dirty trees come back for judgment."
allowed-tools: Bash(scripts/inflight:*), Bash(scripts/reap-worktrees:*), Bash(git:*), Agent
---

You are auditing this machine's worktrees for cleanup. **The census is a
script, not LLM steps** — `scripts/reap-worktrees --report` buckets every
worktree and every branch with no worktree (missing-dir / locked live /
locked dead / dirty / runbook-only / clean+merged incl. the `landed`
sub-count / clean+unmerged, then merged / landed / unique branches),
prints counts plus name lists, and removes nothing. Re-run it with
`--full` when a "+N more" tail needs naming. Your job is to bring each
printed list to Reto for a per-list removal go, then look closer only
where a bucket can't decide.

Live census at invocation:
!`scripts/reap-worktrees --report`

## Procedure

1. **Nothing to do.** If the report shows no removal candidates (no
   missing-dir, no dead-lock, no runbook-only, no clean+merged, no
   merged/landed branchless branches), say so in one line and stop.

2. **Per-list go for the removal candidates.** For each non-empty
   candidate list — `missing dir`, `locked (holder dead)`, `dirty
   (runbook only)`, `clean+merged` (including its `landed` sub-count),
   and the `merged` / `landed` branchless-branch lists — show the names
   and `AskUserQuestion` whether that whole list goes. A yes removes
   every entry in it: `git worktree remove <path>` for worktrees
   (`git worktree unlock` first for the dead-lock list — `remove`
   unconditionally refuses a locked tree), `git branch -D` for
   branchless branches. A no leaves the whole list untouched. Per-list
   means per-list — never cherry-pick entries out of a list silently,
   and never touch the live-lock, dirty, clean+unmerged or unique
   buckets this way.

3. **Judge what the report can't.** `locked (holder live)`, `dirty`
   (real tracked changes), `clean+unmerged` worktrees and `unique`
   branches are informational, not candidates: a live lock means a
   session is in there, and unique commits are unshipped work. If Reto
   asks about a dirty tree, look at the real diff (`git -C <path>
   diff`) and form a view — disposable scratch vs. real unshipped work
   — before offering anything. Never discard uncommitted changes
   without an explicit yes.

4. **Summarize.** One short list: removed / left-live / still-open.
   Don't re-print the full report back at the user.
