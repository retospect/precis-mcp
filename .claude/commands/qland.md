---
description: Quick-land — commit WIP, sync onto main, run ruff+mypy only, squash-merge WITHOUT pytest. For burst-landing many in-flight worktrees under gate congestion; finish the burst with one /go (full gate + deploy). Run from inside a feature worktree.
argument-hint: "[optional commit/ship message]"
allowed-tools: Bash(scripts/ship:*), Bash(git:*), Agent
---

You are quick-landing this worktree: merge to `main` **without pytest**.
This exists for the 20-trees-in-flight case — every `/land` gate competing
for the same Docker VM turns each ship into a queue. `/qland` defers the
suite: qland the burst one by one, then run **one** `/go` (full suite +
deploy) over the integrated `main`.

`scripts/ship --quick` runs its own **pre-qland lint** (ruff autofix · mypy ·
import contracts) before the squash-merge — ~3 min in the warm container, no
test DB, and no fleet gate slot, so it never queues behind a sibling's suite.
Do NOT run ruff, mypy or tests yourself beforehand: the ship does the first
two for you, and running the suite is the congestion this command exists to
avoid. If the pre-qland lint goes red, fix it — it does not get deferred,
because a lint/type failure that lands reddens the *next* tree's gate for a
reason its author did not write.

**The trade you are making, say it out loud in the confirm block:** after a
`/qland`, `main` may be red *at the test level* — no pytest ran. That is
accepted and temporary; the debt is settled by the next full gate.

Live state at invocation:

- Branch + status:
  !`git -c color.ui=never status -sb`
- Commits this branch is ahead of main:
  !`git -c color.ui=never log --oneline origin/main..HEAD 2>/dev/null || git -c color.ui=never log --oneline main..HEAD 2>/dev/null || echo "(can't compute ahead-of-main — neither origin/main nor main resolves)"`

Optional ship message from the user: `$ARGUMENTS`

## Procedure

1. **Decide the message.** Use `$ARGUMENTS` if non-empty; otherwise write a
   concise conventional-commit one-liner for what this branch changes.

2. **Run the tests you added, once.** The pre-qland lint catches the
   lint/type class; it cannot catch a test that was never executed. On
   2026-09-25 a characterization test asserting a bug was still live landed
   onto a main that already carried the fix — the author cannot have run it
   once. If this branch adds or changes tests, run just those
   (`scripts/test tests/test_<file>.py`). One file, seconds, no gate slot —
   not the suite.

3. **Run the script.** Idempotent — re-running after a fix resumes cleanly.
   ```
   scripts/ship --quick "<message>"
   ```
   It does: refuse-if-on-main → commit WIP → ship-lock → sync (`git fetch` +
   `git merge` origin/main) → **pre-qland lint** → squash-merge to `main` via
   `commit-tree` + CAS push → reset the branch to the shipped `main` →
   fast-forward the local `main`. The migration-number and backlog advisories
   still print, and the pre-qland lint (ruff autofix · mypy · import
   contracts) runs and is blocking; pytest and the diff-coverage gate do not.

4. **Handle failures** — merge machinery, plus the pre-qland lint:
   - **Pre-qland lint RED** — ruff, mypy or import contracts. Fix the
     failure printed above the `✖` and re-run `scripts/ship --quick`. Do not
     reach for `PRECIS_QLAND_LINT=0` to get past it; that override is for a
     broken toolchain, not a red check.
   - **Main's last shard verdict is too old** — the burst has outrun its
     verdicts: main's last all-green matrix is 48h+ behind (it warns from
     24h). This is not your tree's problem to fix and re-running will not
     clear it — what it wants is a `/go` over the integrated main, by you or
     by whoever owns the burst. Say so and stop. `PRECIS_QLAND_DRIFT_OVERRIDE=1`
     exists for the case where a `/go` is already running or the staleness is
     known-benign; reaching for it because the refusal is inconvenient is how
     main got 20-odd ungated commits in the first place.
   - **Merge conflict during sync** — resolve, `git add -A && git commit`,
     re-run `scripts/ship --quick`.
   - **CAS push rejected** — a sibling shipped first; just re-run.
   - A `WARNING:` about the primary `main` not fast-forwarding is
     best-effort, not a failure — relay it.

5. **Confirm — always end with this exact block** (verify the sha against
   `git rev-parse origin/main`, don't assume):
   ```
   Merged to main:  ✓ <sha> on origin/main   (or ✗ — ship failed above)
   Gated:           ~ ruff+mypy passed; pytest deferred — main is untested
   Deployed:        — not deployed (run /go after the burst: full gate + deploy)
   ```
   Then one line summarizing what shipped.

6. **Skip the /land ceremony — but carry the debt forward.** No doc-refresh
   pass, no reviewer, no issue-closer here; speed is the point. If this
   branch changed a contract that needs a doc/skill update, or shipped
   something an open gripe/backlog item tracks, note it in one line so the
   post-burst `/go` session settles it. Residual bugs found this session
   still get persisted (`docs/backlog/` / `gripe`) — persistence is never
   skipped, only ceremony.
