---
description: End-of-session wrap-up — commit WIP, sync onto main, gate against the worktree, then squash-merge to main. Runs the deterministic scripts/ship. Run from inside a feature worktree.
argument-hint: "[optional commit/ship message]"
allowed-tools: Bash(scripts/ship:*), Bash(git:*), Bash(docker:*), Bash(uv:*)
---

You are wrapping up a worktree session. The goal of a feature branch is to
land on `main`. **Shipping is a script, not a set of LLM steps** — one
`scripts/ship` run does commit → sync → gate → squash-merge → local-main
fast-forward deterministically, which is faster, reproducible, and
token-cheap. Your job is only to (a) supply a good commit message and
(b) handle anything the script can't (a real merge conflict or a red gate).

Live state at invocation:

- Branch + status:
  !`git -c color.ui=never status -sb`
- Commits this branch is ahead of main:
  !`git -c color.ui=never log --oneline origin/main..HEAD 2>/dev/null || git -c color.ui=never log --oneline main..HEAD 2>/dev/null || echo "(can't compute ahead-of-main — neither origin/main nor main resolves)"`

Optional ship message from the user: `$ARGUMENTS`

## Procedure

1. **Decide the message.** If `$ARGUMENTS` is non-empty, use it. Otherwise
   write a concise one-line, conventional-commit-style summary of what this
   branch changes (look at the diff vs `main` if unsure).

2. **Refresh touched docs (terse, in-place).** For each subsystem this
   branch's diff changes, re-read its `docs/architecture/state-map.md`
   section, the `docs/codebase.md` invariants, and any affected product
   skill under `src/precis/data/skills/`; update in place — terse, per
   `docs/conventions/llm-facing-prose.md` — **only where the change altered
   the contract or shape**, not for every edit. Bump `docs/codebase.md`'s
   `_Verified @ <sha>._` stamp to the tip you're shipping. These edits ride
   the same ship commit (the script auto-commits WIP).

3. **Run the script.** It is idempotent — re-running after a fix resumes
   cleanly.
   ```
   scripts/ship "<message>"
   ```
   `scripts/ship` does, in order: refuse-if-on-main → commit any WIP → sync
   (`git fetch` + `git merge` origin/main) → **integration gate against this
   worktree** in the precis-dev container (it auto-fixes ruff `--fix` +
   `format` and amends them, then runs the authoritative
   `ruff · format · mypy · pytest`) → squash-merge to `main` via `commit-tree`
   + a `--force-with-lease` CAS push → delete the remote feature branch →
   reset the feature branch to the shipped `main` (zero divergence) →
   fast-forward the local `main` → print the new `main` sha.

4. **Handle failures.** The script exits non-zero and prints a `✖` line only
   on something it can't do mechanically:
   - **Merge conflict during sync** — resolve the conflict, then
     `git add -A && git commit`, then re-run `scripts/ship`.
   - **Red gate (mypy/pytest)** — the failure is printed above the `✖`. Ruff
     lint/format drift is auto-fixed, so a ruff failure here means an
     *unfixable* lint error. Fix the code and re-run. Reproduce/iterate on a
     red test locally with `scripts/test <path or -k>` (same container + test
     DB, against this worktree) before re-shipping. Only real failures in
     branch-touched code block the ship; a lone `UniqueViolation` /
     stale-row error in an unrelated test is usually shared-`precis_test`
     pollution — clean the stray row and re-run.
   - **CAS push rejected** — a sibling worktree shipped first; just re-run
     `scripts/ship` (it re-syncs onto the new `main`).
   - A `WARNING:` about the primary `main` not fast-forwarding is
     **best-effort, not a ship failure** (the remote is already updated) —
     just relay it so the human can `git merge --ff-only origin/main` in the
     primary worktree.

5. **Confirm — always end with this exact three-line block** (verify each
   line against `git rev-parse origin/main`, don't assume):
   ```
   Merged to main:  ✓ <sha> on origin/main   (or ✗ — ship failed above)
   Pushed:          ✓ origin/main             (the squash-merge IS the push)
   Deployed:        — not deployed (/endsession is ship-only; run /go to deploy)
   ```
   Use ✗ on the first two lines if the ship failed (red gate / conflict). The
   deploy line is always "not deployed" here. If the local primary `main`
   didn't fast-forward, add the `WARNING:` line as a fourth line. Then one
   line summarizing what shipped.

6. **Follow through on residuals (tiered).** A green ship is not the end if
   this session surfaced latent bugs it parked. **Harvest** every residual
   whose finder is **Opus 4.7 or better** — *this* session (you qualify) or
   an opus reviewer memory (`structural` / `deep_review`). A finding from
   nursery SQL or a haiku planner tick is *filed, never chased* — that is the
   capability gate doing its job. A residual is a concrete correctness gap —
   a latent bug, an incomplete fix, a message-only mitigation of a real root
   cause — **not** a feature extension or a nice-to-have.
   - **Persist first — it must survive compaction.** Before anything else,
     record every harvested residual durably: an `OPEN-ITEMS.md` "Residuals"
     block and/or `kind='todo'` / `gripe` rows. Free-text residuals get
     summarized away on the next auto-compaction; persisted ones don't. The
     persisted list — not your memory — is the source of truth for the loop
     below, so it keeps working after the harness self-compacts.
   - **Fix the in-reach ones now.** For each residual that is a known, bounded
     fix, open a fresh worktree cycle, fix it, and run `/endsession` again
     (ship-only here — `/go` if you also want it deployed). Each residual is
     its own cycle so history stays legible.
   - **File the rest.** Anything that needs investigation before a fix, or is
     out of reach this session, becomes a `kind='todo'` (with `meta.executor`
     where a `fix_gripe` job fits) or a `gripe` — the factory's backlog-groomer
     lane — and you note it; you do not spin on it.
   - **Stop-and-report guard.** If a residual's fix balloons in scope, or goes
     red and isn't quickly greenable, stop, file it, and surface it — never
     chain unbounded ships.

7. **Summarize next steps, then hand off a clean restart.** Close by telling
   the user what — if anything — comes next: the persisted residuals from step
   6, the next item on a tracked list, or "nothing open." Then, when the session
   ran long *or* there are next steps to resume, emit a **full handoff block**
   per `.claude/commands/next.md` step 3 (the copy → `/compact` → paste
   recovery prompt), drawing its pointers from the **persisted** source
   (`OPEN-ITEMS.md` / `kind='todo'` / memory), never a recap of this
   conversation — the durable artifact is what survives compaction. Skip the
   handoff when the session was short and nothing is open; don't manufacture
   ceremony.
