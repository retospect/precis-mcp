---
description: Quick-go — squash-merge to main WITHOUT a gate, deploy that sha to the cluster, then start a repair gate only if a slot is free. The fast dev-cycle path; /go is the slow authoritative one. Run from inside a feature worktree.
argument-hint: "[optional commit/ship message]"
allowed-tools: Bash(scripts/ship:*), Bash(scripts/deploy:*), Bash(scripts/qgo-guard:*), Bash(scripts/mutate-diff:*), Bash(git:*), Bash(docker:*), Monitor, Agent
---

You are shipping **deploy-first**: merge ungated, push it to the fleet, and
let a later pass find what broke. This inverts `/go` deliberately.

**Why this exists.** The full local gate is ~1h15m and the deploy is ~10-20
min. On a day of nine in-flight sessions that made the gate ~85% of cycle
time, and GitHub CI — which runs per-commit, unfiltered, in ~90 min, for free
— caught every red the local gate caught, sooner. On a dev cluster the cost of
shipping a bad worker for twenty minutes is smaller than the cost of not
seeing prod for an hour. So: deploy now, repair from evidence.

**The trade, and say it out loud in the confirm block:** the cluster may run
broken code until the next repair pass or a corrected deploy. That is accepted
on a dev system. It is NOT accepted for changes that production cannot take
back — see the guard in step 1, which is the one thing here you may not skip.

Live state at invocation:

- Branch + status:
  !`git -c color.ui=never status -sb`
- Commits this branch is ahead of main:
  !`git -c color.ui=never log --oneline origin/main..HEAD 2>/dev/null || echo "(none — tree may already equal main)"`
- Irreversibility guard:
  !`scripts/qgo-guard 2>&1 || true`
- Gate slots in use right now:
  !`docker ps --format '{{.Names}}' 2>/dev/null | grep -c 'precis-dev-run' || echo 0`

Optional ship message from the user: `$ARGUMENTS`

## Procedure

1. **Respect the guard — this is the one hard stop.** If `scripts/qgo-guard`
   above printed a refusal, **do not `/qgo`**. It refuses exactly two things,
   both irreversible once deployed:
   - a migration (`src/*/migrations/*.sql`) — forward-only by project rule and
     auto-applied to prod by the precis-web role on every deploy, so a broken
     one writes schema no later gate can undo;
   - `utils/safe_fetch.py` — the SSRF boundary for agent-supplied URLs, which
     the fleet exercises against the open internet the moment it lands.

   Tell the user which it was, and that `/go` is the path for this change
   (gate first, then deploy). Do not argue the guard down, do not add an
   override flag, and do not split the diff to sneak the rest through — if the
   merge contains a migration, the whole merge takes the slow path.

2. **Decide the message.** Use `$ARGUMENTS` if non-empty; otherwise write a
   concise conventional-commit one-liner for what this branch changes.

3. **Merge, untested.**
   ```
   scripts/ship --quick "<message>"
   ```
   Commit WIP → sync → pre-qland lint (ruff autofix · mypy ·
   import contracts · DB-free hygiene tests; no test DB, no gate slot) → ship-lock (seconds) → squash-merge → CAS push. No
   pytest. Failures: the pre-qland lint is blocking — fix what it prints and
   re-run. The rest is merge machinery only: conflict → resolve, add + commit,
   re-run; a lost CAS is retried by ship itself, inside the lock
   (fetch · forward merge · push, ≤5 tries, no re-lint), so only the named
   die after those tries needs a re-run.

4. **Deploy that exact sha.** Read it back rather than assuming:
   ```
   git rev-parse origin/main
   scripts/deploy <that full 40-char sha>
   ```
   There is no `.ship-sha` pin here — only a full gate writes one — so pass the
   sha explicitly. Never pass the branch name `main`: it re-resolves at deploy
   time and a sibling landing in between would send a different tree.
   A successful deploy fast-forwards origin's `prod` ref to it; `gated` does
   not move, so `prod` sits ahead of `gated` until a full gate passes — that
   gap is the published form of "the cluster runs untested code".

   Run it **in the background** and arm a Monitor on the log it announces on
   its first `▶` line, covering milestones AND every failure signature:
   ```
   tail -F "$LOG" | grep -E --line-buffered '^▶|^✖|PLAY \[|fatal:|FAILED|UNREACHABLE|ERROR!|failed=[1-9]'
   ```
   **Read the script's own exit code, not the background task's** — a trailing
   `echo` in the same shell line makes a failed deploy report exit 0. Capture
   it with `; echo "DEPLOY_EXIT=$?"` and check that. If it is non-zero, say so
   plainly: the cluster may be on mixed versions.

5. **Start a repair gate ONLY if a slot is free.** Check the slot count above.
   If it is 0 or 1 (a 2-slot semaphore), launch a background
   `scripts/ship --mutate --full --slow` over the now-deployed main and report
   its result whenever it lands.

   **If both slots are busy, skip it — do not queue.** Queuing a gate per ship
   is what starves the semaphore: with many sessions, gates pile up faster than
   they clear and you get slow *and* ungated, the worst of both (`gr343941`
   recorded an 80-minute wait from exactly this). The scheduled daily
   `--full --slow` over main is the backstop; a skipped repair gate is normal,
   not a failure. Say which happened.

6. **Confirm — always end with this exact block** (verify the sha against
   `git rev-parse origin/main`, don't assume):
   ```
   Merged to main:  ✓ <sha> on origin/main   (or ✗ — ship failed above)
   Gated:           ✗ NOT tested — ruff+mypy passed, /qgo deployed untested code; <repair gate started | repair gate skipped, slots busy>
   Deployed:        ✓ cluster running <sha>   (or ✗ — deploy failed above)
   ```
   The middle line is the point of the command: never render it as a ✓.

7. **Watch prod, since that is what you bought.** The reason to deploy first is
   to see the change running. Say in one line what the user could look at to
   tell whether it worked — a view, a worker lane, a job. If the change has no
   observable prod surface (a library with no caller, docs), say that instead;
   it means `/qgo` bought nothing here and `/qland` would have done.

8. **Residuals still get persisted.** Speed removes ceremony, not memory. A
   latent bug found this session becomes a `docs/backlog/` item or a gripe
   before you finish — free text does not survive compaction. A red repair
   gate is itself a residual: fix it in the next cycle rather than leaving
   main red for a sibling to trip over.
