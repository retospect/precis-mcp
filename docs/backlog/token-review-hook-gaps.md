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
- Two further hooks stay deferred unless pain shows: bare-pytest nudge;
  Stop-with-dirty-worktree reminder.

Owner `scripts/hooks/bash-reflex-nudge.py`; tests `tests/test_bash_reflex_nudge.py`.
