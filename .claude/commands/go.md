---
description: Implement the agreed spec, ship to main, and deploy to the cluster — the dark-factory one-keystroke. Run from inside a feature worktree.
argument-hint: "[optional ship/commit message]"
allowed-tools: Bash(scripts/ship:*), Bash(scripts/deploy:*), Bash(git:*), Bash(docker:*), Bash(uv:*), Agent
---

You said **go**. Turn the spec we've established this session into shipped,
deployed software. Everything mechanical is a script — spend tokens on the
implementation and on genuine failures, nothing else.

Live state at invocation:

- Branch + status:
  !`git -c color.ui=never status -sb`
- Ahead of main:
  !`git -c color.ui=never log --oneline origin/main..HEAD 2>/dev/null || git -c color.ui=never log --oneline main..HEAD 2>/dev/null || echo "(can't compute ahead-of-main — neither origin/main nor main resolves)"`

Optional ship message from the user: `$ARGUMENTS`

## Procedure

1. **Implement the spec.** If the change we discussed this session isn't fully
   written yet, implement it now — code + tests. If it's already done, skip
   straight to shipping. Do not ask for re-confirmation of a spec we've
   already agreed on; that's what "go" means.

2. **Decide the message.** Use `$ARGUMENTS` if non-empty; otherwise write a
   concise conventional-commit one-liner describing what this branch changes.

3. **Refresh touched docs (terse, in-place).** For each subsystem the diff
   changes, re-read its `docs/architecture/state-map.md` section, the
   `docs/codebase.md` invariants, and any affected product skill under
   `src/precis/data/skills/`; update in place — terse, per
   `docs/conventions/llm-facing-prose.md` — **only where the change altered
   the contract or shape**, not for every edit. Bump `docs/codebase.md`'s
   `_Verified @ <sha>._` stamp to the tip you're shipping. These edits ride
   the same ship commit.

4. **Review risky diffs before shipping (size/risk-gated, blocking).** Check
   `git diff --stat origin/main...HEAD` (or `main...HEAD`). Spawn `reviewer`
   (foreground — you need its findings before continuing) only if the diff
   touches **more than ~5 files**, or touches a sensitive area (a migration
   under `src/precis/migrations/`, `safe_fetch.py`, auth/session code, or a
   schema/ACL change). A small, low-risk diff **skips this entirely** — that's
   what on-demand `/code-review` is for; don't spend tokens reviewing the
   common one-line-fix ship. If `reviewer` runs: fix any real finding now,
   before step 5; if you deliberately don't fix one, note why. `clean`, or
   under-threshold, means go straight to step 5.

5. **Ship.** `scripts/ship` is idempotent (re-run resumes cleanly): it does
   commit WIP → sync (`git fetch` + `git merge` main) → the container gate
   (auto-fixes ruff, then authoritative `ruff · format · mypy · pytest`) →
   squash-merge to `main` → reset the branch to the shipped `main` →
   local-main fast-forward.
   ```
   scripts/ship "<message>"
   ```

6. **Handle a red ship.** The script only exits non-zero on something it can't
   do mechanically. **Do NOT deploy if ship failed.**
   - **Red gate (mypy/pytest)** — fix the failure printed above the `✖`,
     re-run `scripts/ship`. Iterate locally with `scripts/test <path or -k>`
     (same container + test DB, against this worktree). (A lone
     `UniqueViolation` in an unrelated test is usually shared-`precis_test`
     pollution — clean the row, re-run.)
   - **Merge conflict** — resolve, `git add -A && git commit`, re-run.
   - **CAS push rejected** — a sibling shipped first; just re-run.
   - A `WARNING:` about the primary main not fast-forwarding is best-effort,
     not a failure — relay it and continue to deploy.

7. **Deploy.** Only after a green ship, push the new `main` to the cluster:
   ```
   scripts/deploy
   ```
   This pings all hosts (aborts on any unreachable — a partial deploy mixes
   versions), then runs the ansible redeploy (reinstalls `precis-mcp@main`
   into every venv and bounces every daemon). It auto-applies pending
   migrations (precis-web role). If it exits non-zero, surface the failing
   ansible task verbatim — the cluster may be on mixed versions; do not
   declare success.

8. **Confirm — always end with this exact three-line block** (verify each
   line, don't assume; check `git rev-parse origin/main` for the sha):
   ```
   Merged to main:  ✓ <sha> on origin/main   (or ✗ — ship failed above)
   Pushed:          ✓ origin/main             (the squash-merge IS the push)
   Deployed:        ✓ cluster running <sha>   (or ✗ — deploy failed above)
   ```
   Use ✗ on any line that did not happen (a red gate → all three ✗; a green
   ship but failed deploy → first two ✓, deploy ✗). If the local primary
   `main` didn't fast-forward, add the `WARNING:` line as a fourth line.

9. **Close what this ship resolved (background, non-blocking) — only if step 8
   confirmed ✓ (merged, regardless of deploy status).** If the ship itself
   failed (✗ on the "Merged to main" line), skip this step entirely — there
   is no new shipped sha to check. Otherwise spawn a background
   `issue-closer` agent with the sha just confirmed in step 8 (not a
   re-derived `git rev-parse main` — use the same sha you verified against
   `origin/main`). It checks open gripes and `OPEN-ITEMS.md` against the
   diff and closes only what it's confident this ship fixed. Don't wait for
   it — continue to step 10 immediately; relay its one-line note (or
   "nothing to close") whenever it completes.

10. **Follow through on residuals (tiered).** A green ship+deploy is not the
    end if this session surfaced latent bugs it parked. **Harvest** every
    residual whose finder is **Opus 4.7 or better** — *this* session (you
    qualify) or an opus reviewer memory (`structural` / `deep_review`). A
    finding from nursery SQL or a haiku planner tick is *filed, never chased*
    — that is the capability gate doing its job. A residual is a concrete
    correctness gap — a latent bug, an incomplete fix, a message-only
    mitigation of a real root cause — **not** a feature extension or a
    nice-to-have.
    - **Persist first — it must survive compaction.** Before anything else,
      record every harvested residual durably: an `OPEN-ITEMS.md` "Residuals"
      block and/or `kind='todo'` / `gripe` rows. Free-text residuals get
      summarized away on the next auto-compaction; persisted ones don't. The
      persisted list — not your memory — is the source of truth for the loop
      below, so it keeps working after the harness self-compacts.
    - **Fix the in-reach ones now.** For each residual that is a known,
      bounded fix, open a fresh worktree cycle, fix it, and run `/go` again
      (ship+deploy). Each residual is its own cycle so history stays legible.
    - **File the rest.** Anything that needs investigation before a fix, or is
      out of reach this session, becomes a `kind='todo'` (with `meta.executor`
      where a `fix_gripe` job fits) or a `gripe` — the factory's backlog-groomer
      lane — and you note it; you do not spin on it.
    - **Stop-and-report guard.** If a residual's fix balloons in scope, or goes
      red and isn't quickly greenable, stop, file it, and surface it — never
      chain unbounded ship+deploys.

11. **Summarize next steps, then hand off a clean restart.** Close by telling
    the user what — if anything — comes next: the persisted residuals from step
    10, the next item on a tracked list, or "nothing open." Then, when the
    session ran long *or* there are next steps to resume, emit a **full
    handoff block** per `.claude/commands/next.md` step 3 (the copy →
    `/compact` → paste recovery prompt), drawing its pointers from the
    **persisted** source (`OPEN-ITEMS.md` / `kind='todo'` / memory), never a
    recap of this conversation — the durable artifact is what survives
    compaction. Skip the handoff when the session was short and nothing is
    open; don't manufacture ceremony.
