---
description: Quick-land — commit WIP, sync onto main, squash-merge WITHOUT running the gate. For burst-landing many in-flight worktrees under gate congestion; finish the burst with one /go (full gate + deploy). Run from inside a feature worktree.
argument-hint: "[optional commit/ship message]"
allowed-tools: Bash(scripts/ship:*), Bash(git:*), Agent
---

You are quick-landing this worktree: merge to `main` **without** the
container gate. This exists for the 20-trees-in-flight case — every `/land`
gate competing for the same Docker VM turns each ship into a queue. `/qland`
skips validation entirely and defers it: qland the burst one by one, then run
**one** `/go` (full suite + deploy) over the integrated `main`.

**The trade you are making, say it out loud in the confirm block:** after a
`/qland`, `main` may be red (lint, types, tests — nothing ran). That is
accepted and temporary; the debt is settled by the next full gate. Do NOT
"helpfully" run tests, ruff, or mypy first — that recreates exactly the
congestion this command exists to avoid.

Live state at invocation:

- Branch + status:
  !`git -c color.ui=never status -sb`
- Commits this branch is ahead of main:
  !`git -c color.ui=never log --oneline origin/main..HEAD 2>/dev/null || git -c color.ui=never log --oneline main..HEAD 2>/dev/null || echo "(can't compute ahead-of-main — neither origin/main nor main resolves)"`

Optional ship message from the user: `$ARGUMENTS`

## Procedure

1. **Decide the message.** Use `$ARGUMENTS` if non-empty; otherwise write a
   concise conventional-commit one-liner for what this branch changes.

2. **Run the script.** Idempotent — re-running after a fix resumes cleanly.
   ```
   scripts/ship --quick "<message>"
   ```
   It does: refuse-if-on-main → commit WIP → ship-lock → sync (`git fetch` +
   `git merge` origin/main) → **no gate** → squash-merge to `main` via
   `commit-tree` + CAS push → reset the branch to the shipped `main` →
   fast-forward the local `main`. The migration-number and backlog advisories
   still print; ruff/mypy/pytest do not run.

3. **Handle failures** — only genuine merge machinery can go red here:
   - **Merge conflict during sync** — resolve, `git add -A && git commit`,
     re-run `scripts/ship --quick`.
   - **CAS push rejected** — a sibling shipped first; just re-run.
   - A `WARNING:` about the primary `main` not fast-forwarding is
     best-effort, not a failure — relay it.

4. **Confirm — always end with this exact block** (verify the sha against
   `git rev-parse origin/main`, don't assume):
   ```
   Merged to main:  ✓ <sha> on origin/main   (or ✗ — ship failed above)
   Gated:           ✗ deferred — /qland ran NO gate; main is unvalidated
   Deployed:        — not deployed (run /go after the burst: full gate + deploy)
   ```
   Then one line summarizing what shipped.

5. **Skip the /land ceremony — but carry the debt forward.** No doc-refresh
   pass, no reviewer, no issue-closer here; speed is the point. If this
   branch changed a contract that needs a doc/skill update, or shipped
   something an open gripe/backlog item tracks, note it in one line so the
   post-burst `/go` session settles it. Residual bugs found this session
   still get persisted (`docs/backlog/` / `gripe`) — persistence is never
   skipped, only ceremony.
