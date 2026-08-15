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

test: hook unit tests on the new patterns.
