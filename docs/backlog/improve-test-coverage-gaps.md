# Test-coverage gaps, in value order

From the 2026-08-02 review: (a) `asa_bot` — 6/13 modules untested incl.
`bot.py` message loop and `pg_listen.py` reconnect/backoff; (b) the
untested web routes, ops-facing `gripes.py` + `clusters.py` first;
(c) `workers/executors/claude_docker.py` claim/spawn path; (d)
spot-check handler/utils modules with no test-name match
(`_todo_guards.py`, `conversation.py`, `compile_guard.py`,
`_claude_subprocess.py` stand out). Sonnet-shaped, slice it.
