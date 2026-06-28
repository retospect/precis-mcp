---
description: End-of-session wrap-up — commit WIP, sync onto main, run the integration gate against the worktree, then squash-merge the branch to main. Run from inside a feature worktree.
argument-hint: "[optional commit/ship message]"
allowed-tools: Bash(git:*), Bash(docker:*), Bash(./scripts/dev:*), Bash(scripts/dev:*), Bash(uv:*), Bash(rm:*)
---

You are wrapping up a worktree session. The goal of a feature branch is
to land on `main` — this command takes it there: **commit → sync (merge
main in) → integration gate → squash-merge to main**.

> **Why this command exists instead of `git town ship`.** `git town ship`
> does `git checkout main`, which **always fails from a linked worktree**
> (the primary worktree already has `main` checked out:
> `fatal: 'main' is already used by worktree at …`). And `./scripts/dev`
> bind-mounts the **main** repo at `/app` (hard-coded in the compose
> file), so a naive gate tests `main`, **not** your worktree edits. Both
> are worked around below: the gate bind-mounts *this* worktree over
> `/app`, and the ship is done with git plumbing (`commit-tree` + a
> compare-and-swap push) that never touches the primary's working tree or
> index. This is also race-safe against sibling worktree sessions shipping
> concurrently onto the shared `.git`.

Live state at invocation:

- Branch + status:
  !`git -c color.ui=never status -sb`
- Commits this branch is ahead of main:
  !`git -c color.ui=never log --oneline origin/main..HEAD 2>/dev/null || git -c color.ui=never log --oneline main..HEAD`
- git-town parent / ship strategy:
  !`git config --get-regexp '^git-town' | grep -Ei "ship-strategy|$(git branch --show-current)\.parent" || echo '(no git-town parent set — step 1 sets it)'`

Optional ship message from the user: `$ARGUMENTS`

## Procedure

Work through these steps in order. **If any step fails, STOP, report
exactly what failed, and do not proceed** — never ship red, never ship a
branch that isn't on top of the latest `main`. The session closes right
after this command, so don't worry about leaving the worktree tidy
post-ship.

1. **Sanity.** Confirm you're on a feature branch (NOT `main`). If you're
   on `main`, stop and report — there's nothing to ship. If git-town has
   no parent recorded for this branch (`git town sync` errors with
   *"cannot determine parent branch"*), set it before syncing:
   ```
   git config git-town-branch.$(git branch --show-current).parent main
   ```

2. **Commit WIP.** If the working tree is dirty, stage everything and
   commit it so nothing is lost. Use `$ARGUMENTS` as the message if
   given, else write a concise message summarizing the uncommitted work.
   End the commit message with:
   `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

3. **Sync onto main.** Run `git town sync`. For a feature branch this is a
   **merge** (not a rebase): it fetches and merges the latest `origin/main`
   into this branch, then pushes. Notes:
   - On a merge conflict, resolve it, `git town continue`. If you can't
     resolve confidently, stop and report.
   - If you **amend** a commit *after* a sync has already pushed the
     branch (e.g. the format auto-fix in step 4), the next `git town sync`
     will merge `origin/<branch>` (the pre-amend commit) back in and
     conflict against your amend. Resolve by taking your local version
     (`git checkout --ours <file> && git add <file>`), then
     `git town continue`. The squash-ship in step 6 collapses the merge
     commits away, so this is cosmetic.

4. **Integration gate** — run it **against this worktree**, in the
   container, by bind-mounting `$PWD` over `/app` (the default
   `./scripts/dev` mount would test `main` instead). First drop any
   host-built virtualenv so it doesn't shadow the container's:
   ```
   rm -rf .venv
   UID="$(id -u)" GID="$(id -g)" docker compose \
     -f "${PRECIS_COMPOSE:-$HOME/work/infrastructure/compose.yaml}" \
     --profile dev run --rm --no-deps -v "$PWD":/app precis-dev \
     bash -lc 'uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest -q'
   ```
   - If `ruff format --check` reports drift **only in files this branch
     touched** (`git diff --name-only origin/main..HEAD`), apply
     `uv run ruff format <those files>` (the container's ruff version is
     authoritative — host ruff may differ), amend it into the branch, and
     re-run the gate. That's the one auto-fix you may make.
   - Drift / failures in files this branch did **not** touch are
     pre-existing on `main` — report them as a warning, do not reformat
     unrelated files into the ship commit.
   - Any `ruff check`, `mypy`, or `pytest` failure in branch-touched code:
     **abort and report.** The human fixes, then re-runs `/endsession`.
   - **Test-DB caveat.** The container's `PRECIS_TEST_PG_URL` (from
     `.secrets.env`) uses the low-privilege `precis` role against the
     shared `precis_test` DB. If the gate errors at fixture setup with
     `permission denied for table _migrations`, someone ran host `pytest`
     as the `postgres` superuser against `precis_test` and changed table
     ownership — re-grant or recreate `precis_test`, don't paper over it
     by switching the gate to the superuser. A lone `UniqueViolation` /
     stale-row failure in an unrelated test (e.g. `embedders`,
     `worker_logs`) is usually shared-`precis_test` pollution, not your
     regression — clean the stray row and re-run that test to confirm.

5. **Re-sync if needed.** If the gate took a while, run `git town sync`
   once more so the ship lands on the very latest `main`. (No-op if
   nothing moved.) Confirm the branch sits on top of main:
   `git fetch -q origin main && git merge-base --is-ancestor origin/main HEAD && echo SYNCED`.

6. **Ship — squash-merge via plumbing** (do **not** run `git town ship`;
   it fails from a worktree, see the header). Build a single squash commit
   whose tree is the branch's working tree and whose parent is the current
   `origin/main`, then compare-and-swap it onto `main`:
   ```
   git fetch -q origin main
   OLD_MAIN=$(git rev-parse origin/main)
   TREE=$(git rev-parse HEAD^{tree})
   # SAFETY: the squash must contain only this branch's changes. Confirm
   # the diff vs main is what you expect before pushing:
   git --no-pager diff --stat "$OLD_MAIN" HEAD
   NEW=$(git commit-tree "$TREE" -p "$OLD_MAIN" -F - <<'MSG'
   <one-line summary>   # use $ARGUMENTS if given, else summarize the branch

   <optional body>
   Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
   MSG
   )
   # Compare-and-swap: refuses if origin/main moved since the fetch above
   # (a sibling session shipped first) — re-run from step 5 if it does.
   git push --force-with-lease=main:"$OLD_MAIN" origin "$NEW":refs/heads/main
   ```
   Then delete the now-merged feature branch on the remote (the local
   branch + worktree are torn down with the session):
   ```
   git push origin --delete "$(git branch --show-current)" || true
   ```

7. **Report.** Print the new `main` commit
   (`git fetch -q origin main && git log --oneline -1 origin/main`) and a
   one-line summary of what shipped. Optionally verify a key symbol from
   the change is present (`git show origin/main:<file> | grep <symbol>`).
   Note any pre-existing-drift warnings from step 4 so they can be cleaned
   up on `main` separately.
