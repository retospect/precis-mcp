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

test: hook unit tests on the new patterns.
