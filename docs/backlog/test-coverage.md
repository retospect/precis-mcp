# Test coverage

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Test-coverage gaps, in value order

_Grouped 2026-09-26; was `improve-test-coverage-gaps`._

From the 2026-08-02 review: (a) `asa_bot` — 6/13 modules untested incl.
`bot.py` message loop and `pg_listen.py` reconnect/backoff; (b) the
untested web routes, ops-facing `gripes.py` + `clusters.py` first;
(c) `workers/executors/claude_docker.py` claim/spawn path; (d)
spot-check handler/utils modules with no test-name match
(`_todo_guards.py`, `conversation.py`, `compile_guard.py`,
`_claude_subprocess.py` stand out). Sonnet-shaped, slice it.

Related: improve-route-sql-tests.md (focused SQL companions for the raw-SQL
web routes).

## Real-PG route-SQL test companions — close the audited gap

_Grouped 2026-09-26; was `improve-route-sql-tests`._

Policy: docs/conventions/testing.md. 2026-08-02 audit: of 18 web routes
with raw SQL, 5 have real-PG coverage, 12 are FakeStore-only, and
`routes/agentlogs.py` has no tests at all. Write
`tests/precis_web/test_<module>_sql.py` companions (shape:
`test_status_sql.py`), ranked by SQL volume: tasks (9 raw calls),
preview, clusters, categorizers, cad (6 each), factory (5), drafts (3),
agentlogs (2 — do first despite rank), then the 5 single-query modules
(refs, papers, gripes, asks, alerts). Each test executes every raw query
once incl. adversarial `%`/`_` input. Sonnet-shaped, batchable.

Related: improve-test-coverage-gaps.md (route-sql-tests is the sharper
real-PG version of its untested-web-routes slice).
