# token-review: bash-reflex-nudge misses real traffic; compact-thrash re-reads

Transcript sample (6 largest sessions) shows the nudges don't move behavior:
(a) Rule A matches only bare-identifier greps while real traffic is
multi-pattern tree-wide greps — add an exploratory-grep rule nudging
search_code/navigator, rate-limited per session; (b) Rule B fires per call
but never escalates — count per session, escalate after ~5, and wrap the
retyped DSN-extraction boilerplate as a helper (`scripts/agent-dsn`?);
`_SSH_RE` misses spark/castor/pollux; (c) post-compact sessions re-Read the
same governing doc — extend the PreCompact nudge to ask for a state-so-far
note naming the doc + line ranges. Owner `scripts/hooks/bash-reflex-nudge.py`,
`scripts/hooks/precompact-persist.sh`. Two further hooks stay deferred unless
pain shows: bare-pytest nudge; Stop-with-dirty-worktree reminder.

2026-08-15 pass adds (d) **Rule D**: `sed -n '<N>,<M>p' <local-file>` used
instead of Read `offset`/`limit` (36–63 calls/session, ~72K tokens of
tool_result across just two sessions) and `echo "=== label ==="` narration
wrapping compound Bash probes (308/420 Bash calls in the worst session, 73%) —
both named in CLAUDE.md but outside Rules A–C and outside rtk's rewrite list
(`sed` isn't a known command, so the reflex is invisible to compression AND
nudging). Nudge `sed -n` range-reads on existing local paths toward
`Read(offset/limit)`, and flag any Bash command containing a literal
`echo "===` independently (cheaper detection, no path resolution).
Concentrated in primary-checkout orchestration sessions, not worktree feature
sessions. The 07-29 fixes are holding (cluster-ops delegation + coderef nudge
confirmed working in the same sample).

2026-08-23 pass adds two more, from the 6 largest sessions since 08-15
(19.5MB–5.3MB; `echo "==="` narration is now essentially gone — 0/6 files —
so that half of Rule D shipped; `sed -n` is not fixed, still 2–76 calls/file):
(e) **Rule E**: raw `Bash tail -N .../tasks/<id>.output` polling for
background-job status instead of the `Monitor` tool, often bundled with a
`git log`/`git rev-parse` probe in the same call — ~111 raw polls vs. 9
`Monitor` calls across the 6-file sample (3 of 6 files used `Monitor` zero
times). Nudge repeated `tail .../tasks/*.output` on the same path within a
session toward `Monitor`. (f) **Rule F**: session `b30f9d07` (web-basic-auth
feature build) ran almost entirely un-delegated — 366 raw `Bash`, 104 `Edit`,
32 `Write`, **zero** `coder` dispatches for standard multi-file feature work
(`auth.py` edited 22×, `users.py` 16×, `test_auth.py` 13×), plus 34 raw
`ssh melchior` calls with zero `cluster-ops`/`cluster-admin` dispatch (Rule B
confirmed still firing-but-ignored, at unusually high volume in this one
session). Candidate: a PreToolUse-on-Edit nudge that counts same-session
Edit/Write calls against feature-shaped files and suggests `coder` dispatch
past a threshold — orthogonal to the existing Bash-only rules, so likely a
separate hook. Not yet confirmed as a repeat-session pattern (single session
so far); watch the next pass for recurrence before committing to the hook.

test: hook unit tests on the new patterns.
