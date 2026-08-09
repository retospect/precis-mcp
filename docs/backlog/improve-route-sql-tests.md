# Real-PG route-SQL test companions — close the audited gap

Policy: docs/conventions/testing.md. 2026-08-02 audit: of 18 web routes
with raw SQL, 5 have real-PG coverage, 12 are FakeStore-only, and
`routes/agentlogs.py` has no tests at all. Write
`tests/precis_web/test_<module>_sql.py` companions (shape:
`test_status_sql.py`), ranked by SQL volume: tasks (9 raw calls),
preview, clusters, categorizers, cad (6 each), factory (5), drafts (3),
agentlogs (2 — do first despite rank), then the 5 single-query modules
(refs, papers, gripes, asks, alerts). Each test executes every raw query
once incl. adversarial `%`/`_` input. Sonnet-shaped, batchable.
