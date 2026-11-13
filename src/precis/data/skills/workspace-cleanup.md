---
id: workspace-cleanup
family: work
title: precis — reap finished worktrees and branches
summary: audit this machine's worktrees and branchless branches for cleanup — the census script buckets them, each candidate list gets one per-list go from the user, judgment only where a bucket cannot decide
answers:
  - how do I clean up worktrees and branches left in flight?
  - which worktrees are safe to remove and which need a closer look?
  - how do I prove a worktree path free of canonical-checkout and container-mount references before removing it?
  - what do I do with a dirty or locked worktree?
applies-to: auditing and reaping worktrees and branches on this machine
status: active
tags: [workflow]
governs: factory
---

# workspace-cleanup — reap finished worktrees

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

Harness-neutral: the per-list confirmation below is `AskUserQuestion` in
the Claude harness — use your runtime's ask-the-user primitive; Kimi maps
commands via `/skill:<name>`.

## Procedure

1. **Nothing to do.** If the report shows no removal candidates (no
   missing-dir, no dead-lock/idle-lock, no runbook-only, no clean+merged, no
   merged/landed branchless branches), say so in one line and stop.

2. **Per-list go for the removal candidates.** For each non-empty
   candidate list — `missing dir`, `locked (holder dead)`, `dirty
   (runbook only)`, `clean+merged` (including its `landed` sub-count),
   and the `merged` / `landed` branchless-branch lists — show the names
   and `AskUserQuestion` whether that whole list goes. A yes removes
   every entry in it: `git worktree remove <path>` for worktrees
   (`git worktree unlock` first for the dead-lock/idle-lock list — `remove`
   unconditionally refuses a locked tree), `git branch -D` for
   branchless branches. A no leaves the whole list untouched. Per-list
   means per-list — never cherry-pick entries out of a list silently,
   and never touch the live-lock, dirty, clean+unmerged or unique
   buckets this way. Before each `git worktree remove`, run
   `deploy/mcp-http/precis-mcp-http-ensure.sh --check-path <path>`: only
   exit 10 proves the path free of canonical-checkout marker and container
   mount references. Exit 0 means in use; every other result means the
   probe could not prove it free (including missing Docker or failed Docker
   queries). Preserve the path and report the reason unless the result is
   10 (gr516960: a strays cleanup once deleted the recorded checkout out
   from under the live backends). This probe does not replace the removal
   authorization above.

3. **Judge what the report can't.** `locked (holder live)`, `dirty`
   (real tracked changes), `clean+unmerged` worktrees and `unique`
   branches are informational, not candidates: a live lock means a
   session is in there, and unique commits are unshipped work. If Reto
   asks about a dirty tree, read its capped `diffstat` from
   `scripts/inflight --json` (the harness refuses `git -C` into another
   tree; `docs/conventions/container-ops.md`) and form a view —
   disposable scratch vs. real unshipped work — before offering
   anything. Never discard uncommitted changes
   without an explicit yes.

4. **Summarize.** One short list: removed / left-live / still-open.
   Don't re-print the full report back at the user.

## See also

- [[whatneedsdoing]] — the triage view where inflight trees also surface.
- [[next]] — the handoff that leaves a worktree resumable.
- [[fleet]] — the orchestrator's own tree-and-window bookkeeping.
