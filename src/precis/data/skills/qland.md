---
id: qland
family: work
title: precis — quick-land the branch without pytest
summary: burst-landing lane — commit, sync, pre-qland lint (ruff + secret scan + fast DB-free guards), squash-merge without pytest; the debt is settled by one go after the burst
answers:
  - how do I land many worktrees without each one queueing for the full gate?
  - which checks does scripts/ship --quick actually run, and which are deferred to the release gate?
  - what do I do when qland refuses because main's last verdict is too old?
  - what debt does a qland leave on main and who settles it?
applies-to: burst-landing many worktree branches to main — the fast lane that defers pytest to the release gate
status: active
tags: [workflow]
scope: write
governs: factory
---

# qland — quick-land without pytest

You are quick-landing this worktree: merge to `main` **without pytest**.
This exists for the 20-trees-in-flight case — every `land` gate competing
for the same Docker VM turns each ship into a queue. `qland` defers the
suite: qland the burst one by one, then run **one** `go` (release cut
from the integrated `main`, CI-gated, deployed).

`scripts/ship --quick`: ruff + secret scan + fast DB-free guards; mypy,
import contracts and all
pytest run at the release gate (Reto 2026-10-30: "Don't even bother with
python checks, we'll do it all as a batch in the release branch"). The
**pre-qland lint** (ruff autofix · the secret-tree scan, kept because the
repo is public · the fast guards — skill vocabulary, render, type-ignore
ratchet, next-literal ratchet, ruff scripts-excludes, scripts-use-uv — ~30 s, no test DB) runs before the squash-merge and no fleet gate slot, so it never queues behind a sibling's suite.
Do NOT run ruff, mypy or tests yourself beforehand: the ship does ruff for
you, mypy, import contracts and pytest run at the release gate, and running the suite is the congestion this command exists to
avoid. If the pre-qland lint goes red, fix it — it does not get deferred,
because a lint failure that lands reddens the *next* tree's gate for a
reason its author did not write.

**The trade you are making, say it out loud in the confirm block:** after a
`qland`, `main` may be red *at the test, type and import-contract level* — no pytest, mypy or lint-imports ran. That is
accepted and temporary; the debt is settled by the next full gate.

Live state at invocation:

- Branch + status:
  !`git -c color.ui=never status -sb`
- Commits this branch is ahead of main:
  !`git -c color.ui=never log --oneline origin/main..HEAD 2>/dev/null || git -c color.ui=never log --oneline main..HEAD 2>/dev/null || echo "(can't compute ahead-of-main — neither origin/main nor main resolves)"`

Optional ship message from the user: `$ARGUMENTS`

The `!`-prefixed command lines are the Claude harness's live-state injection
— other runtimes run those commands themselves (Kimi invokes this skill as
`/skill:qland`).

## The checked lane (opt-in)

**`scripts/ship --checked` is an opt-in, not the default lane** (Reto
2026-10-30, reaffirmed 2026-11-05: land with plain `--quick` as each tree
finishes, no batching; pytest runs once, in the release gate). The checked
mode (gr485791) is the same machinery
as `--quick`, but its lint adds mypy, import contracts and the hygiene tests, and it runs a selected pytest — the
contract invariants (audited tables, closed registries, ratchets,
vocab/secret scans) plus corpus-coupled tests for data files and the
index/testmon set for Python changes — all selected by
`scripts/impacted-tests`, the same selector the release lane uses. That is
a couple of minutes instead
of nothing; use it when the diff touches something those tests guard.
`qland` itself keeps running `--quick`; invoke `scripts/ship --checked`
directly, and check the same selection beforehand with
`scripts/test --selected`.

## Procedure

1. **Decide the message.** Use `$ARGUMENTS` if non-empty; otherwise write a
   concise conventional-commit one-liner for what this branch changes. The
   message is required (`-m`, or derived from the branch's own commit); ship
   refuses `ship(...)`/`wip(...)`/unprefixed subjects before linting.

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
   It does: refuse-if-on-main → refuse a dirty tree (outside `.claude/`) → sync (`git fetch`,
   squash the branch to one commit, `git rebase` onto origin/main — linear, no merge
   commit) → **pre-qland lint** (unlocked) → ship-lock (seconds;
   if main moved, `git merge` it forward inside the lock, lint not re-run, a
   `Gate: lint only` trailer on the squash) → squash-merge to `main` via
   `commit-tree` + CAS push → fast-forward the local `main` → release the lock →
   reset the branch to the shipped `main`. The migration-number and backlog advisories
   still print, and the pre-qland lint (ruff autofix · secret scan · fast
   DB-free guards · mypy on the changed `src/`/`tests/` files only —
   the `PRECIS_SHIP_QUICK_MYPY` env opts out of just that leg) runs and is
   blocking; full mypy, import contracts, the other hygiene tests, pytest and the
   diff-coverage gate do not (the release gate runs them).

## Handle failures

4. **Handle failures** — merge machinery, plus the pre-qland lint:
   - **Pre-qland lint RED** — ruff, the secret scan, mypy on a changed file or a fast guard (the
     `FAILED tests/...` line names it). Fix the
     failure printed above the `✖` and re-run `scripts/ship --quick`. Do not
     reach for the `PRECIS_QLAND_LINT` env override to get past it; that
     override is for a broken toolchain, not a red check.
   - **Main's last shard verdict is too old** — the burst has outrun its
     verdicts: main's last all-green matrix is 48h+ behind (it warns from
     24h). This is not your tree's problem to fix and re-running will not
     clear it — what it wants is a `go` over the integrated main, by you or
     by whoever owns the burst. Say so and stop. The
     `PRECIS_QLAND_DRIFT_OVERRIDE` env
     exists for the case where a `go` is already running or the staleness is
     known-benign; reaching for it because the refusal is inconvenient is how
     main got 20-odd ungated commits in the first place.
   - **Merge conflict during sync** — resolve, `git add -A && git commit`,
     re-run `scripts/ship --quick`.
   - **CAS push rejected** — ship retries inside the lock itself (fetch ·
     forward merge · push, ≤5 tries, no re-lint); only the named die after
     those tries needs a re-run.
   - A `WARNING:` about the primary `main` not fast-forwarding is
     best-effort, not a failure — relay it.

## Confirm

5. **Confirm — always end with this exact block** (verify the sha against
   `git rev-parse origin/main`, don't assume):
   ```
   Merged to main:  ✓ <sha> on origin/main   (or ✗ — ship failed above)
   Gated:           ~ ruff + secret scan + fast guards passed; mypy, import contracts and pytest deferred to the release gate — main is untested
   Deployed:        — not deployed (run /go after the burst: gated release + deploy)
   ```
   Then one line summarizing what shipped.

   If the squash subject named `(grN/tdM)` refs, ship already moved them —
   the `land-notify` line prints next to `SHIP_STATUS`; check the ref's
   state, not the log, and never post a second `landed:` comment by hand
   (gr542762).

## Carry the debt forward

6. **Skip the land ceremony — but carry the debt forward.** No doc-refresh
   pass, no reviewer, no issue-closer here; speed is the point. If this
   branch changed a contract that needs a doc/skill update, or shipped
   something an open gripe/backlog item tracks, note it in one line so the
   post-burst `go` session settles it. Residual bugs found this session
   still get persisted (a backlog item / `gripe`) — persistence is never
   skipped, only ceremony.

## See also

- [[go]] — the gated release + deploy that settles the qland debt.
- [[qgo]] — deploy-first fast lane; qland is merge-only.
- [[rebase]] — sync this branch onto main before the ship.
- [[flow-stages]] — the stage router that picks land vs qland vs go.
