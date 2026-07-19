---
description: Fast-forward or rebase the current worktree's branch onto main. Deterministic script for the clean path; LLM steps in only to resolve real conflicts or ask you.
argument-hint: "[optional base ref, default origin/main]"
allowed-tools: Bash(scripts/rebase:*), Bash(git:*)
---

You are syncing this worktree's branch onto its base (default `origin/main`).
**The mechanical path is a script, not LLM steps** — `scripts/rebase` does
fetch → up-to-date check → fast-forward-or-rebase deterministically, which is
fast and token-cheap. Your job is only to (a) run it and (b) handle what it
can't finish alone: a real merge conflict (resolve it cleverly, or ask the
user) or a diverged trunk.

Live state at invocation:

- Branch + status:
  !`git -c color.ui=never status -sb`
- Commits this branch is ahead of origin/main:
  !`git -c color.ui=never log --oneline origin/main..HEAD 2>/dev/null | head -20 || echo "(origin/main not resolved yet — the script will fetch)"`

Optional base ref from the user: `$ARGUMENTS`

## Procedure

1. **Run the script.** Pass `$ARGUMENTS` as the base if the user gave one,
   otherwise no argument (defaults to `origin/main`).
   ```
   scripts/rebase $ARGUMENTS
   ```
   It ends with a `REBASE_STATUS: <token>` line. Branch on it:

2. **Clean path — nothing more to do.** For `uptodate`, `ff`, or `clean`
   (exit 0), the sync completed mechanically. Report it in **one line** (what
   the branch now sits on: `git rev-parse --short HEAD` / `origin/main`) and
   stop. Do not re-run anything.

3. **`conflict` (exit 10) — resolve, cleverly.** The rebase is stopped
   mid-flight with conflict markers in the tree. This is the LLM half.
   - Look at what's conflicted: `git -c color.ui=never status -sb` and
     `git -c color.ui=never diff --diff-filter=U`. For context on each side,
     read the conflicting commit (`git show HEAD` = the commit being replayed,
     `git show HEAD@{1}` / the base for the other side) and the surrounding
     file.
   - **Resolve only what you can do correctly.** Mechanical, unambiguous
     conflicts — import-order clashes, both sides adding adjacent lines, a
     lock/generated file, whitespace, one side clearly superseding the other —
     resolve directly by editing the file to the correct combined result, then
     `git add <file>`. Keep BOTH sides' intent unless one genuinely obsoletes
     the other; never drop a hunk to make markers disappear.
   - **Ask when it's a judgment call.** If a conflict is semantically
     meaningful — two real logic changes to the same code, a schema/contract
     divergence, anything where picking wrong silently breaks behavior — use
     **AskUserQuestion**: show the two sides tersely and ask which to take (or
     describe the merge you propose). Do not guess on behavior-changing
     conflicts.
   - When a commit's conflicts are all staged: `git rebase --continue`
     (it won't open an editor for you to fill — reuse the existing message).
     The rebase may stop again on a later commit; repeat this loop until it
     finishes cleanly.
   - After a clean finish, confirm with `git -c color.ui=never log --oneline
     origin/main..HEAD` and report the one-line result.
   - **Bail-out guard.** If the conflicts are broad, entangled, or you're not
     confident, don't force it — run `git rebase --abort` (restores the
     pre-rebase branch exactly, autostash reapplied), tell the user what
     conflicts you saw and why you stopped, and let them decide. Never leave
     the tree in a half-resolved rebase.

4. **`diverged` (exit 10) — on main, can't fast-forward.** The current branch
   is `main`/`master` and has local commits not on the base, so it won't
   fast-forward. Don't auto-rebase the trunk. Show the divergence
   (`git log --oneline --left-right origin/main...HEAD`) and ask the user how
   to proceed (rebase local main onto origin/main, reset to origin/main, or
   leave it) via AskUserQuestion.

5. **`error` (exit 1) — hard failure.** The script prints a `✖` reason
   (detached HEAD, fetch failed, unresolved base…). Relay it and, if the fix
   is obvious and safe (e.g. check out a branch), offer it; otherwise ask.
