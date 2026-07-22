---
description: Review every worktree/branch in flight on this machine and clean up what's safe. Deterministic data collection (scripts/inflight --json) + a Sonnet agent to confirm-and-remove the clear-cut cases; ambiguous dirty worktrees come back here for judgment.
allowed-tools: Bash(scripts/inflight:*), Bash(git:*), Agent
---

You are auditing this machine's worktrees for cleanup. **The data collection
is a script, not LLM steps** — `scripts/inflight --json` already computes,
per worktree: session liveness (a live pid vs. a dead lock), dirty status, and
merge verdict (cherry-pick-aware — squash-merges don't fool it), and buckets
each one into `base` / `self` / `live_session` / `needs_judgment` /
`safe_remove` / `has_unmerged_work`. Your job is to route the buckets to the
right place, not to re-derive any of this by hand.

Live state at invocation:
!`scripts/inflight --json`

## Procedure

1. **Nothing to do.** If every worktree above is `base`, `self`, or
   `live_session` (no `safe_remove`, no `needs_judgment`, no
   `has_unmerged_work`), say so in one line and stop.

2. **Delegate the clear-cut cases.** If there's at least one `safe_remove`
   entry, spawn the `housekeeper` agent (Sonnet tier) with the full JSON
   payload above. It will `AskUserQuestion` the user on which `safe_remove`
   worktrees to reap, execute the unlock/remove/branch-delete for confirmed
   ones, and report back what it removed plus every `needs_judgment` /
   `has_unmerged_work` entry it left untouched (verbatim, with diffstats).
   If there's no `safe_remove` entry but there IS a `needs_judgment` or
   `has_unmerged_work` one, skip the agent — there's nothing for it to do —
   and go straight to step 3 with the live JSON.

3. **Judge the ambiguous ones yourself.** For each `needs_judgment` worktree
   (from the housekeeper's report, or directly from the JSON if you skipped
   step 2), look at its `diffstat`. If it's not obviously enough to decide,
   read the real diff (`git -C <path> diff`) and form a view: disposable
   scratch vs. real unshipped work. This is the one judgment call in this
   flow that stays here — don't ask the housekeeper agent to make it, and
   don't guess without looking. Tell the user what's in there and your read,
   then `AskUserQuestion` how they want to proceed (leave it, commit it,
   discard it). Never discard uncommitted changes without an explicit yes.

4. **Note `has_unmerged_work` entries** (real commits ahead of base, not
   merged) as informational only — these are active branches, not cleanup
   candidates, unless the user says otherwise.

5. **Summarize.** One short list: removed / left-live / resolved-ambiguous /
   still-ahead. Don't re-print the full JSON back at the user.
