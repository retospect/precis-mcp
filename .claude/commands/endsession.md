---
description: End-of-session wrap-up — commit WIP, rebase onto main, run the integration gate, then squash-merge the feature branch back to main (git-town ship). Run from inside a feature worktree.
argument-hint: "[optional commit/ship message]"
allowed-tools: Bash(git:*), Bash(./scripts/dev:*), Bash(scripts/dev:*), Bash(uv:*)
---

You are wrapping up a worktree session. The goal of a feature branch is
to land on `main` — this command takes it there: **commit → sync (rebase
onto main) → integration gate → squash-merge to main**, using the repo's
git-town config (`ship-strategy = squash-merge`, feature branches parented
on `main`).

Live state at invocation:

- Branch + status:
  !`git -c color.ui=never status -sb`
- Commits this branch is ahead of main:
  !`git -c color.ui=never log --oneline main..HEAD`
- git-town parent / ship strategy:
  !`git config --get-regexp '^git-town' | grep -Ei "ship-strategy|$(git branch --show-current)\.parent" || echo '(no git-town config found)'`

Optional ship message from the user: `$ARGUMENTS`

## Procedure

Work through these steps in order. **If any step fails, STOP, report
exactly what failed, and do not proceed** — never ship red, never ship a
branch that isn't on top of the latest `main`. The session closes right
after this command, so don't worry about leaving the worktree tidy
post-ship.

1. **Sanity.** Confirm you're on a feature branch (NOT `main`), and that
   git-town shows this branch parented on `main`. If you're on `main` or
   there's no git-town parent, stop and report — there's nothing to ship.

2. **Commit WIP.** If the working tree is dirty, stage everything and
   commit it so nothing is lost. Use `$ARGUMENTS` as the message if
   given, else write a concise message summarizing the uncommitted work.
   End the commit message with:
   `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

3. **Sync onto main.** Run `git town sync`. This pulls the latest `main`
   and rebases this branch onto it. If it stops on a conflict, resolve
   the conflicts, finish the rebase, then continue — if you can't resolve
   them confidently, stop and report.

4. **Integration gate** (container-first, per `AGENTS.md`):
   ```
   ./scripts/dev bash -lc 'uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest'
   ```
   - If `ruff format --check` reports drift **only in files this branch
     touched** (`git diff --name-only main..HEAD`), apply
     `uv run ruff format <those files>`, amend it into the branch, and
     re-run the gate. That's the one auto-fix you may make.
   - Drift / failures in files this branch did **not** touch are
     pre-existing on `main` — report them as a warning but do not treat
     them as this branch's regression, and do not reformat unrelated
     files into the ship commit.
   - Any `ruff check`, `mypy`, or `pytest` failure in branch-touched code:
     **abort and report.** The human fixes, then re-runs `/endsession`.

5. **Re-sync if needed.** If the gate took a while, run `git town sync`
   once more so the ship lands on the very latest `main`. (No-op if
   nothing moved.)

6. **Ship.** Run `git town ship`. With `ship-strategy = squash-merge`
   this squash-merges the branch into `main` as a single commit, pushes,
   and removes the feature branch. If `git town ship` prompts for a
   commit message, use `$ARGUMENTS` if given, else a one-line summary of
   the branch's net change.

7. **Report.** Print the new `main` commit (`git -C <main-worktree> log
   --oneline -1` or `git log --oneline -1 main`) and a one-line summary of
   what shipped. Note any pre-existing-drift warnings from step 4 so they
   can be cleaned up on `main` separately.
