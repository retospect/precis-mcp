---
status: idea
pillar: platform
title: scripts/inflight classifies the running deploy's render worktree as safe_remove and prints a removal command for it — the likely cause of the 2026-09-27 mid-deploy FileNotFoundError
---

# `scripts/inflight` tells agents to delete the tree a running deploy is reading from

## What

`scripts/deploy` renders `deploy/` templates from a throwaway detached
worktree at `<git-common-dir>/precis-deploy-tree` whenever the target is a
literal sha — which is the normal `/go` path, since `scripts/ship` pins the
gated sha (`scripts/deploy:389-400`). Ansible reads its playbooks from that
tree for the whole run, minutes at a time.

`scripts/inflight` enumerates worktrees and buckets them. It has no
knowledge of that tree: it is clean, it is merged, it has no live session,
so it falls into `safe_remove` (`scripts/inflight:172`) and the summary
prints

    Removable (merged + clean + no live session): precis-deploy-tree
      → review, then: git worktree remove <name> && git branch -d worktree-<name>

`git worktree remove` deletes the directory. So the table that every session
is shown at start, and that agents are told to scan, actively recommends
deleting the working set of any in-flight pinned deploy. There is no check
for `<git-common-dir>/precis-deploy.lock`, which is held for the duration of
a deploy (`scripts/deploy:314`) and would answer the question exactly.

## Why this is more than theoretical

The 2026-09-27 18:39Z deploy of `6008588c` died mid-run with

    [ERROR]: Task failed: [Errno 2] No such file or directory
    Origin: .../.git/precis-deploy-tree/deploy/playbooks/33-precis-agent-image.yml:524:11
    (source not shown: FileNotFoundError)

Ansible was still polling an async build task when its own playbook file
went away. Established about that window:

- The render tree existed at ~18:50Z — an `inflight` run listed it, at
  `6008588c`, and named it under "Removable".
- It was gone by 19:03:03Z, when ansible raised, and is still gone.
- The only code in the repo that deletes that path is `scripts/deploy`
  itself: the leftover sweep at startup (`:394-395`) and the EXIT trap
  (`:344-347`). The trap runs *after* the failure it would have to explain,
  so it is not the cause. `scripts/ship` never touches it — no reference to
  the path, no `worktree remove`, no `prune` (its only `rm -rf` calls are the
  container's `/app` and its own lock dir).
- Ruled out by the session that ran the deploy: the sha-scoped build dir and
  the build wrapper on melchior are both still present, so the missing file
  was on the controller, not the host.

That leaves an external `worktree remove` in the 18:50–19:03 window, which is
precisely what `inflight` was recommending for that path at 18:50.
Not proven — no session has admitted to it, and the two sessions asked both
deny it — but it is the only mechanism that fits, and the recommendation is a
defect whether or not it fired here.

## Fix

1. **`scripts/inflight` must never bucket the deploy render tree as
   removable.** Cheapest correct version: exclude any worktree whose path is
   `<git-common-dir>/precis-deploy-tree` outright — it is never a session's
   work and never something a janitor should reap. Better, and it also covers
   future throwaway trees: treat the tree as live while
   `<git-common-dir>/precis-deploy.lock` exists.
2. **The same hazard exists for `scripts/deploy`'s own startup sweep.** Its
   comment reads "the deploy lock is already held, so a leftover here is a
   dead run's, not a live one's" (`:391-393`). That holds only as far as the
   lock does, and the lock's dead-holder steal uses `kill -0` (`:319`), which
   cannot distinguish a dead pid from a live pid owned by another user — this
   host runs deploys as both `reto` and `deploy`. A cross-user deploy would
   therefore steal a live lock and then `rm -rf` the live render tree, giving
   the identical signature. Worth either recording the holder's user
   alongside its pid, or making the steal require a positive liveness check
   rather than the absence of one.
3. **Separately, the failure should not be silent.** Ansible losing its
   playbook mid-run surfaced only as `[Errno 2]` with no indication that the
   render tree had vanished. `scripts/deploy` could assert the tree still
   exists when ansible exits non-zero and say so plainly.

## See also

`agent-image-build-stalls-on-mirror-fallback.md` — the same incident from the
build's side: the 900 s progress watchdog did fire on a wedged
`apt`/nodesource fetch, and the controller died before the retries ladder or
the trail slurp could run.
