---
status: draft
pillar: platform
---

# token-review: bash-reflex-nudge misses real traffic; compact-thrash re-reads

Open gaps from the token-review passes (2026-07 .. 2026-10-02). Shipped:
`sed -n` slice → Read nudge (`bash-reflex-nudge.py` Rule D) and the
50-edit delegation nudge (`scripts/hooks/edit-delegation-nudge.py`, Rule F).

- (a) **Rule A** matches only bare-identifier greps while real traffic is
  multi-pattern tree-wide greps — add an exploratory-grep rule nudging
  search_code/navigator, rate-limited per session.
- (b) **Rule B** fires per call but never escalates — count per session,
  escalate after ~5, and wrap the retyped DSN-extraction boilerplate as a
  helper (`scripts/agent-dsn`?); `_SSH_RE` misses spark/castor/pollux.
- (c) Post-compact sessions re-Read the same governing doc — extend the
  PreCompact nudge to ask for a state-so-far note naming the doc + line
  ranges. Owner `scripts/hooks/precompact-persist.sh`. Compact thrash:
  10-22 auto-compacts in each of the top 8 sessions (10-02 pass).
- (d) **Rule D, `echo "==="` half**: narration wrapping compound Bash probes
  is not hook-coded (only CLAUDE.md prose) and regressed to 8-9% of Bash calls
  in the worst sessions (09-02 pass). Flag any Bash command containing a
  literal `echo "===` (cheap detection, no path resolution). Also open:
  re-slicing the same file — 613 of 2758 sed/Read-by-path calls hit a path
  already read 3+ times in that session (10-02 pass); a per-session
  per-path counter on Read could nudge.
- (e) **Rule E**: raw `Bash tail -N .../tasks/<id>.output` polling instead of
  the `Monitor` tool. Ratio improved (12:1 → 1.75-3:1 in most sessions, still
  19:1 and 6:1 in two); no `TaskOutput` re-poll loops left (10-02). Nudge
  repeated `tail .../tasks/*.output` on the same path toward `Monitor` if the
  next pass doesn't converge further.
- (f) **Wrong tier**: `c6fc74be` ran four `general-purpose` agents on the
  `fable` model whose whole job was wording gripe text (gripe-filer / haiku
  remit).
- **2026-11-05 pass** (14 d, 1,559 local files, 75k de-duplicated turns):
  - (c) is now the main cost: main-loop Opus ran 35.6k turns at a mean 203k
    context, ~50% of weighted spend in main-loop cache reads alone. The
    gripe-fix orchestrator `e19078a0` ran 17,254 turns over 12 days with 73
    context drops and a 755k peak; fan-out (1,001 subagents) was 38% of its
    cost, the re-read context the rest (gr515153). A recycle/handoff at
    ~150k and moving relay/census polling off the Opus loop is the lever.
  - (g) **zsh glob abort**: 834 `no matches found` results, 444 retried on
    the next call. The hook already detects the unquoted glob but only
    annotates; the command still runs and fails. Deny pre-run with the
    quoted form, or `setopt nonomatch` for harness shells.
  - (h) **Worktree-isolation guard**: 440 refusals — 213 agents typing
    `rtk proxy git <read-only>`, 169 "too complex to verify". The guard is
    harness built-in (gr456287, wontfix) and `docs/conventions/rtk.md`
    already names the passing forms (`RTK_DISABLED=1 git …`,
    `/usr/bin/git …`); the line is not being read. The repo lever is a
    `bash-reflex-nudge` rule that answers a typed `rtk proxy git` with the
    passing form before the guard refuses it.
  - (f) 304 `general-purpose` and ~46 `claude` dispatches, 170 `reviewer`
    calls with a `model=opus` override on a sonnet definition; `navigator`
    was reached 8 times. Census, triage and land-a-branch are the repeated
    jobs.
  - (e) improved to 0.87:1 tail:Monitor; 316 `sleep` commands with 83
    blocked-then-retried remain. (d) `echo "==="` is down to 0.7% of Bash.
  - Git output is 30.6 MB of 82.8 MB Bash bytes (`git show` p95 15.9 kB,
    `git diff` p95 21 kB) — no cap or filter on either. Read: 381 whole-file
    reads over 20 kB (13.2 MB); one log was Read 68 times as a poll.
- Codex fleet transcripts are outside the `scripts/token-review` corpus
  (`docs/runbooks/token-review.md`); if the Codex fleet still runs, the
  miner should separate provider counters and read them (from the closed
  fleet-token-efficiency-audit, 2026-10-21).
- Two further hooks stay deferred unless pain shows: bare-pytest nudge;
  Stop-with-dirty-worktree reminder.

Owner `scripts/hooks/bash-reflex-nudge.py`; tests `tests/test_bash_reflex_nudge.py`.
