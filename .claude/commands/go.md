---
description: "Implement the agreed spec, land it to main, cut a release branch from main, gate it on CI and deploy it. Run from a feature worktree."
argument-hint: "[optional ship/commit message]"
allowed-tools: Bash(scripts/ship:*), Bash(scripts/release:*), Bash(scripts/round:*), Bash(scripts/test:*), Bash(scripts/deploy:*), Bash(fleet:*), Bash(gh:*), Bash(cat:*), Bash(git:*), Bash(docker:*), Bash(uv:*), Bash(tail:*), Agent, Monitor, TaskStop
---

You said **go**. Turn the spec we've established this session into shipped,
deployed software: land to main, cut a release branch from main, gate that
exact sha on CI, deploy it. Everything mechanical is a script — spend tokens on the
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
   The message is required: ship takes it from `-m`/this argument or reuses the
   branch's own commit, and refuses `ship(...)`/`wip(...)`/unprefixed subjects.

3. **Refresh touched docs (terse, in-place).** For each subsystem the diff
   changes, re-read the owning package docstring, the
   `docs/codebase.md` invariants, and any affected product skill under
   `src/precis/data/skills/`; update in place — terse, per
   `docs/conventions/llm-facing-prose.md` — **only where the change altered
   the contract or shape**, not for every edit. These edits ride
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

5. **Land to main.** `scripts/ship --quick` is idempotent: refuse a dirty tree →
   sync onto main → lint (ruff · mypy · import contracts · DB-free hygiene
   tests) → squash-merge. No pytest here: the authoritative gate is CI on the
   release cut in step 7, which tests main at a frozen sha, so later main
   pushes cannot change what you deploy.
   ```
   scripts/ship --quick "<message>"
   ```
   Before landing, run the tests you touched with `scripts/test <path or -k>`
   — a red release blocks every session's next deploy, not only yours.
   Release CI has no diff-coverage gate yet
   (`docs/backlog/release-ci-diff-coverage.md`): changed `src/` lines need
   tests all the same. When the tree already equals `origin/main`
   (everything qlanded), ship lands nothing; go straight to step 7 — `/go`
   is also the settle-up after a `/qland` burst.

6. **Handle a red land.** Lint red: fix what is printed above the `✖`,
   re-run. Merge conflict: resolve, `git add -A && git commit`, re-run. CAS
   push rejected: ship forward-merges and retries itself. A `WARNING:` about
   the primary main not fast-forwarding is best-effort — relay it and
   continue. **Do not release if the land failed.**

7. **Release — cut, gate, deploy.** `scripts/release` takes S =
   `origin/main` (your landed sha plus whatever siblings landed before it)
   and drives the round machinery: open a round if none is open (close and
   reopen one that already deployed), `scripts/round cut --sha S` →
   `release/r<N>`, wait for check.yml's full gate (lint + 6 shards) on that
   exact head, then `scripts/round deploy`, which deploys the head
   `--pinned` and moves `gated`/`prod`. Run it in the background, output to
   a log:
   ```
   scripts/release > .claude/release.log 2>&1; echo "RELEASE_EXIT=$?" >> .claude/release.log
   ```
   Wait on `^RELEASE_EXIT=` (Bash `run_in_background`), and arm a Monitor on
   the log for `^▶|^✖|PLAY \[|fatal:|FAILED|UNREACHABLE|ERROR!|failed=[1-9]|queued|riding|red`
   so silence is never ambiguous. The deploy step still tees to the main
   checkout's `.deploy-logs/<ts>-<ref>.log`.

   | Exit | Meaning | Do |
   |---|---|---|
   | 0 | S already in `origin/prod`, or the in-flight release contains S (`riding …`) | nothing to deploy; that release ships it |
   | 1 | refused: S off main's first-parent line, or a cut guard (migration-number collision, `gated`/`prod` cannot fast-forward) | read the refusal; fix on main, re-run |
   | 2 | release CI red, or not green by the timeout | see below |
   | 3 | rollout succeeded; runtime confirmation pending | step 8 |
   | 4 | another session's release stayed in flight past the queue timeout | S is on main; re-run `scripts/release` later |
   | other | `scripts/deploy` failed; the pin is retained | surface the failing ansible task verbatim — the cluster may be on mixed versions. Fix, re-run `scripts/release`; it resumes |

   **Red release (exit 2).** The log names the failing jobs and the
   `gh run view <id> --log-failed` line; read its `FAILED` / `E  ` lines.
   The release branch stays and blocks other sessions' releases until it is
   fixed or abandoned. Classify flake vs real first — OOM-137, colima
   bind-mount and shared test-DB failures look like reds but aren't code: a
   flake is `gh run rerun <id> --failed`, then re-run `scripts/release`. A
   real failure is fixed onto the release from a fresh tree cut from
   `origin/release/r<N>` holding only the fix, with
   `scripts/ship --release "<subject>"` (`.claude/commands/round.md`
   §Release fixes), then re-run `scripts/release`. If the fix is out of
   reach: `scripts/round cut --abandon` (only once everything on the
   release is on main), file it, say so.

   Concurrency is ride-along: one release at a time. A `/go` whose sha is in
   the in-flight release exits 0; one whose sha is not waits for that release
   to retire, then cuts the next. Only the worktree that cut the release
   resumes it (CI wait, deploy, runtime confirmation) — it holds the
   gitignored `.release-owner` marker, removed after `round close`. If the
   cutting session died, `scripts/release --adopt` from another tree takes
   ownership; say so. The marker binds `scripts/release` only — a hand-run
   `scripts/round deploy`/`--confirm-runtime` ignores it, so don't run those
   on a release another tree owns. Main pushes after the cut never touch the
   open release. Between 03:00 and 04:20 UTC, deploy refuses a release that
   adds a migration beyond prod; re-run after the window.

8. **Verify, confirm, close.** On exit 3, verify the runtime: the deploy's
   `origin/prod →` line names the sha; `get(kind='skill', id='precis-status')`
   reports that `git_sha`, the expected migration and the kinds you had
   before; every required daemon's boot sha and readiness match, including
   the serving worker. Then run the command the log's `next:` line prints:
   ```
   scripts/round deploy --confirm-runtime <S> --runtime-evidence "<services, boot SHAs, readiness, session MCP>" && scripts/round close
   ```
   That publishes `deployed/r<N>`, deletes the release branch and closes
   the round. Every deploy restarts the shared session MCP: `fleet say` the
   sha, restart time and "verify a write landed before retrying". End with
   this exact three-line block (verify each line against `origin/main` and
   `origin/prod`):
   ```
   Merged to main:  ✓ <sha> on origin/main   (or ✗ — land failed above)
   Released:        ✓ release/r<N> at <S>, CI green   (or ✗ / riding r<N> / queued)
   Deployed:        ✓ cluster running <S>     (or ✗ — deploy failed above)
   ```
   If the local primary `main` didn't fast-forward, add the `WARNING:` line
   as a fourth line.

9. **Close what this ship resolved (background, non-blocking) — only if step 8
   confirmed ✓ (merged, regardless of release/deploy status).** If the ship itself
   failed (✗ on the "Merged to main" line), skip this step entirely — there
   is no new shipped sha to check. Otherwise spawn a background
   `issue-closer` agent with the sha just confirmed in step 8 (not a
   re-derived `git rev-parse main` — use the same sha you verified against
   `origin/main`). It checks open gripes and `docs/backlog/` against the
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
    - **Confusion-mined residuals: deploy-date-check FIRST.** A "latent bug"
      pulled from a prod transcript or worker log may already be fixed —
      transcripts persist forever, so an `[error:…]` in a "recent" job can be an
      old failure echoed, or quoted text (see `whatneedsdoing` step 6's two
      false-positive traps). Before persisting *or* investigating one,
      `git log -S'<callsite>'` for a fix and confirm the real occurrences
      postdate its deploy; if every occurrence predates the fix, it's already
      fixed — resolve per the gripe convention (comment naming the sha →
      soft-delete), don't file or chase it. (Skipping this once sent an
      already-fixed `edit()`-° incident to a full root-cause pass — `gr175738`.)
    - **Persist first — it must survive compaction.** Before anything else,
      record every harvested residual durably: a `docs/backlog/` item
      and/or `kind='todo'` / `gripe` rows. Free-text residuals get
      summarized away on the next auto-compaction; persisted ones don't. The
      persisted list — not your memory — is the source of truth for the loop
      below, so it keeps working after the harness self-compacts.
    - **Fix the in-reach ones now.** For each residual that is a known,
      bounded fix, open a fresh worktree cycle, fix it, and run `/go` again
      (ship+deploy). Each residual is its own cycle so history stays legible.
    - **File the rest.** Anything that needs investigation before a fix, or is
      out of reach this session, becomes a `gripe` (filed against the owning thread, whose
      session fixes it) or a `kind='todo'` — the fix_gripe lane is off — and
      you note it; you do not spin on it.
    - **Stop-and-report guard.** If a residual's fix balloons in scope, or goes
      red and isn't quickly greenable, stop, file it, and surface it — never
      chain unbounded ship+deploys.

11. **Summarize next steps, then hand off a clean restart.** Close by telling
    the user what — if anything — comes next: the persisted residuals from step
    10, the next item on a tracked list, or "nothing open." Then, when the
    session ran long *or* there are next steps to resume, emit a **full
    handoff block** per `.claude/commands/next.md` steps 4–5 (the copy →
    `/compact` → paste recovery prompt), drawing its pointers from the
    **persisted** source (`docs/backlog/` / `kind='todo'` / memory), never a
    recap of this conversation — the durable artifact is what survives
    compaction. Skip the handoff when the session was short and nothing is
    open; don't manufacture ceremony.
