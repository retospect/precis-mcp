---
status: draft
title: Auto-reap deleted a LIVE session's worktree twice — unlock-before-check + non-pid locks read as removable
model: opus
---

# Reap deleted a live worktree (2026-08-15, session pid 22516)

`swift-drifting-sky` was deleted out from under its live session **twice
within ~20 minutes** of a ship leaving it merged+clean (session pid 22516
alive throughout, verified `kill -0`). Both in-flight `taproot-migrate
reground` runs against prod died mid-run (`claude -p` Bun ENOENT on the
deleted cwd, `cannot import name` from the vanished source tree — the
error-sentinel path caught all of it, artifact safely unusable). No
uncommitted work was lost — post-ship the tree was clean — but only by
luck of timing.

## Defect 1 — `session-end-reap.sh` unlocks then reaps on a spurious SessionEnd

`scripts/hooks/session-end-reap.sh` releases the SessionStart lock
**unconditionally** (before the bucket check, by design per its header
comment) on any SessionEnd whose `reason` isn't `clear`/`resume`, then
removes the tree itself if the freshly-unlocked tree buckets
`safe_remove`. One spurious SessionEnd event carrying this worktree's cwd
— fired while the session was demonstrably alive (deletion #1 landed
between two of its tool calls) — is sufficient for both unlock AND
removal in a single hook run. Open question for the fix: **which event
fired SessionEnd with reason ∉ {clear, resume} mid-session?** Suspects:
subagent/background-task teardown, or post-compaction session cycling
(this session had just auto-compacted). Reproduce/trace before patching;
the `reason` allowlist is the likely hole (`other` is treated as a
genuine end).

- Fix sketch: a SessionEnd must not unlock/reap while the lock's `pid <N>`
  is alive **unless the payload identifies the ending session as the
  locker** (session-id or pid in the payload, compared to the lock
  reason). `kill -0` alone can't discriminate — a genuinely-ending
  session's pid is still alive at hook time — so the payload comparison
  is the load-bearing part.

## Defect 2 — a lock without `pid <N>` in its reason is treated as removable

`scripts/inflight` `session_field()` parses liveness ONLY from
`pid <N>` in the lock reason; any other locked reason renders as
`locked`, and the removable condition (`sess != live#*`) treats that as
no-live-session → `safe_remove`. `scripts/reap-worktrees` (and defect
1's path) then **unlock first** (dead-lock handling) and remove. Net: a
manually-locked worktree with a human-readable reason is *less*
protected than an unlocked dirty one. Deletion #2 was exactly this — a
recovery lock with reason "live session 22516 …" (no `pid` keyword).

- Fix sketch: treat ANY lock whose reason doesn't parse as a dead
  `pid <N>` as `needs_judgment`, never `safe_remove` — unknown-format
  locks were placed deliberately by someone; reap must not guess.

## Collateral: the session's stdio precis MCP wedged

Hours after the deletions, the same session's `precis serve` stdio MCP
stopped responding entirely — two `link` calls and then a trivial
`get(kind='skill')` all sat silent past the 1800s idle timeout, while
direct psql to the same prod DSN worked fine. The serve process was
started before the worktree was deleted/recreated twice under it, so a
stale-cwd/stale-inode import or a crashed in-process worker thread
holding the request loop are the suspects. Recovery: reconnect the MCP
server (session restart or `/mcp` reconnect); diagnose with
`uvx py-spy dump` on the serve pid if it recurs.

## Interim mitigations (in use by the affected session)

- Re-lock with the exact parseable reason `pid <N>` (live#-classified,
  protected from defect 2).
- An untracked `REAP-GUARD.tmp` at the worktree root keeps the tree
  dirty — dirty never buckets `safe_remove` on either path, which also
  covers defect-1 repeats (spurious unlock). Must be deleted before
  shipping or `scripts/ship` auto-commits it.

test: a SessionEnd payload whose session identity does not match the
worktree lock's `pid <N>` leaves the lock held and the tree in place; a
worktree locked with a non-`pid` reason is never bucketed `safe_remove`
by inflight (and reap-worktrees consequently never unlocks/removes it).
