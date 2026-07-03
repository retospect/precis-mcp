---
description: Implement the agreed spec, ship to main, and deploy to the cluster — the dark-factory one-keystroke. Run from inside a feature worktree.
argument-hint: "[optional ship/commit message]"
allowed-tools: Bash(scripts/ship:*), Bash(scripts/deploy:*), Bash(git:*), Bash(docker:*), Bash(uv:*)
---

You said **go**. Turn the spec we've established this session into shipped,
deployed software. Everything mechanical is a script — spend tokens on the
implementation and on genuine failures, nothing else.

Live state at invocation:

- Branch + status:
  !`git -c color.ui=never status -sb`
- Ahead of main:
  !`git -c color.ui=never log --oneline origin/main..HEAD 2>/dev/null || git -c color.ui=never log --oneline main..HEAD`

Optional ship message from the user: `$ARGUMENTS`

## Procedure

1. **Implement the spec.** If the change we discussed this session isn't fully
   written yet, implement it now — code + tests. If it's already done, skip
   straight to shipping. Do not ask for re-confirmation of a spec we've
   already agreed on; that's what "go" means.

2. **Decide the message.** Use `$ARGUMENTS` if non-empty; otherwise write a
   concise conventional-commit one-liner describing what this branch changes.

3. **Ship.** `scripts/ship` is idempotent (re-run resumes cleanly): it does
   commit WIP → `git town sync` → the container gate (auto-fixes ruff, then
   authoritative `ruff · format · mypy · pytest`) → squash-merge to `main` →
   local-main fast-forward.
   ```
   scripts/ship "<message>"
   ```

4. **Handle a red ship.** The script only exits non-zero on something it can't
   do mechanically. **Do NOT deploy if ship failed.**
   - **Red gate (mypy/pytest)** — fix the failure printed above the `✖`,
     re-run `scripts/ship`. (A lone `UniqueViolation` in an unrelated test is
     usually shared-`precis_test` pollution — clean the row, re-run.)
   - **Merge conflict** — resolve, `git town continue`, re-run.
   - **CAS push rejected** — a sibling shipped first; just re-run.
   - A `WARNING:` about the primary main not fast-forwarding is best-effort,
     not a failure — relay it and continue to deploy.

5. **Deploy.** Only after a green ship, push the new `main` to the cluster:
   ```
   scripts/deploy
   ```
   This pings all hosts (aborts on any unreachable — a partial deploy mixes
   versions), then runs the ansible redeploy (reinstalls `precis-mcp@main`
   into every venv and bounces every daemon). It auto-applies pending
   migrations (precis-web role). If it exits non-zero, surface the failing
   ansible task verbatim — the cluster may be on mixed versions; do not
   declare success.

6. **Confirm — always end with this exact three-line block** (verify each
   line, don't assume; check `git rev-parse origin/main` for the sha):
   ```
   Merged to main:  ✓ <sha> on origin/main   (or ✗ — ship failed above)
   Pushed:          ✓ origin/main             (the squash-merge IS the push)
   Deployed:        ✓ cluster running <sha>   (or ✗ — deploy failed above)
   ```
   Use ✗ on any line that did not happen (a red gate → all three ✗; a green
   ship but failed deploy → first two ✓, deploy ✗). If the local primary
   `main` didn't fast-forward, add the `WARNING:` line as a fourth line.
