---
description: End-of-session wrap-up — commit WIP on the feature branch, sync onto main, gate in the container, squash-merge to main. Runs the deterministic scripts/ship. Ship-only; /go also deploys.
---

You are wrapping up a session. The goal of a feature branch is to land on
`main`. **Shipping is a script, not a set of LLM steps** — one `scripts/ship`
run does commit → sync → gate → squash-merge → local-main fast-forward
deterministically. Your job is only to (a) supply a good commit message and
(b) handle anything the script can't (a real merge conflict or a red gate).

The user may pass a ship message after the command; otherwise write one.

## Procedure

1. Check live state:
// turbo
   `git -c color.ui=never status -sb && git -c color.ui=never log --oneline origin/main..HEAD`
   You must be on a feature branch — `scripts/ship` refuses to run on `main`.

2. **Decide the message.** Use the user's argument if given; otherwise write a
   concise one-line conventional-commit summary of what this branch changes
   (look at the diff vs `main` if unsure).

3. **Refresh touched docs (terse, in-place).** For each subsystem this
   branch's diff changes, re-read its `docs/architecture/state-map.md`
   section, the `docs/codebase.md` invariants, and any affected product skill
   under `src/precis/data/skills/`; update in place — terse, per
   `docs/conventions/llm-facing-prose.md` — **only where the change altered
   the contract or shape**. Bump `docs/codebase.md`'s `_Verified @ <sha>._`
   stamp. These edits ride the same ship commit (the script auto-commits WIP).

4. **Run the script** (idempotent — re-running after a fix resumes cleanly):
   ```
   scripts/ship --impacted "<message>"
   ```
   `--impacted` narrows the gate's **pytest** to testmon's affected-tests set
   (ruff/mypy still full) — the fast `/land` path; `/go` runs the full suite
   before a deploy. First `--impacted` ship on a fresh worktree runs everything
   once to build the map, then later ones are the fast selection.

   It does: refuse-if-on-main → commit WIP → sync (`git fetch` + `git merge`
   origin/main) → container gate (auto-fix ruff, then
   `ruff · format · mypy · pytest`) → squash-merge to `main` via `commit-tree`
   + `--force-with-lease` CAS push → reset the branch to shipped `main` →
   fast-forward local `main` → print the new sha.

5. **Handle failures** (script exits non-zero only on what it can't do
   mechanically):
   - **Merge conflict during sync** — resolve, `git add -A && git commit`,
     re-run `scripts/ship`.
   - **Red gate (mypy/pytest)** — the failure prints above the `✖`. Iterate
     with `scripts/test <path or -k>` before re-shipping. A lone
     `UniqueViolation` in an unrelated test is usually shared-`precis_test`
     pollution — clean the stray row and re-run.
   - **CAS push rejected** — someone shipped first; just re-run (it re-syncs).
   - A `WARNING:` about local `main` not fast-forwarding is best-effort, not a
     ship failure — relay it.

6. **Confirm — always end with this exact three-line block** (verify against
   `git rev-parse origin/main`, don't assume):
   ```
   Merged to main:  ✓ <sha> on origin/main   (or ✗ — ship failed above)
   Pushed:          ✓ origin/main             (the squash-merge IS the push)
   Deployed:        — not deployed (/land is ship-only; run /go to deploy)
   ```

7. **Follow through on residuals.** A residual is a concrete correctness gap
   surfaced this session (latent bug, incomplete fix) — not a nice-to-have.
   - **Persist first**: record each in an `OPEN-ITEMS.md` "Residuals" block so
     it survives the session. Filing a `gripe`/`todo` via the precis MCP
     **writes PROD** — do that only with the user's explicit go-ahead.
   - **Fix in-reach ones now**: each on a fresh feature branch, then /land
     again. If a fix balloons in scope, stop, file it, surface it.

8. **Summarize next steps**: persisted residuals, the next backlog item, or
   "nothing open."
