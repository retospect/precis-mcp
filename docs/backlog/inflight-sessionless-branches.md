---
status: ready
title: scripts/inflight + reapers are blind to branches that have no worktree
---

`scripts/inflight` (and therefore `reap-worktrees`, the SessionStart
in-flight table, and `/workspace-cleanup`) iterate `git worktree list
--porcelain` only (`scripts/inflight` — the `done < <(git worktree list
--porcelain)` loop). A local branch whose worktree was removed but whose
branch was kept is invisible to all of them, so its unmerged commits sit
stranded with no session, no purpose line, and no reap advisory.

Observed 2026-09-16: `feat/se-datum-measure-eval` — 3 commits, 14 files,
~1.4k lines (se datum selectors + evaluator + 2 migrations), 8 h old, no
worktree — surfaced only because a human asked "do we have dead branches?"
and a hand-run `git branch --format='%(worktreepath)'` showed the empty
column. Its plugin-migration number had meanwhile collided with a live
tree's (`precis_se/migrations/0011`), which the migration-number advisory
could not warn about either, since it is scoped to trees the table knows.

## Change

1. After the worktree loop, `git for-each-ref refs/heads` and emit one row
   per branch with an empty `%(worktreepath)` that is not `main`:
   `WORKTREE` = `(no worktree)`, `SESSION` = `—`, `VS-main` via the same
   `--cherry-pick --right-only` count the table already uses (squash-merge
   breaks plain `ahead`), `PURPOSE` = newest branch-unique commit subject.
2. `reap-worktrees`: a sessionless, worktree-less branch whose cherry-pick
   count is 0 is safe to `git branch -D` under the same rule that reaps a
   merged+clean+sessionless worktree; a non-zero count prints a
   `stranded:` advisory, never deletes.
3. Migration-number advisory: include those branches' `migrations/*.sql`
   in the collision scan.

## Test

`tests/test_inflight*.py` (or the script's own `--json` self-check): a
temp repo with one branch checked out nowhere and one unmerged commit →
the JSON carries it with `worktree: null`; with the commit squash-merged
→ `reap-worktrees --dry-run` lists it under safe_remove.
