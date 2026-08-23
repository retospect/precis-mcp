---
status: draft
title: Raw SQL in precis_web routes is unverified — FakeStore renders a nonexistent column green
prio: high
---

# Raw SQL in precis_web routes is unverified — FakeStore renders a nonexistent column green

## Motivation / why

`tests/precis_web/` runs almost entirely against the web `FakeStore`
(`tests/precis_web/conftest.py`), which never parses SQL. A route that
hand-writes a query naming a column that does not exist renders a green
test and a 500 in prod.

This is not hypothetical. `a4775105` fixed exactly that: `routes/asks.py`
`::_project_draft` projected `r.slug` from `refs` — a column `refs` has
never had (the agent-facing slug lives in `ref_identifiers` as
`id_kind='cite_key'`, sourced by the Ref mapper's correlated subquery,
`store/_mappers.py::_REFS_COLS`). It took the whole `/needs-you` landing
down with `UndefinedColumn` for every ask that fell back to the
project-level draft, and no test noticed because `_project_draft` had no
test at all and its siblings only ever saw the fake.

The existing mitigation is a hand-written `test_*_sql.py` per surface
(`test_tags_sql`, `test_status_sql`, `test_drive_sql`, `test_structure_sql`,
`test_smartdraft_sql`, and now `test_asks_sql`). That only covers SQL
someone already suspected — it cannot flag the query nobody thought about,
which is the failure mode that actually ships.

## In scope

A mechanical check that every raw SQL string literal reachable from a
`precis_web` route is at least *parseable against the real schema* —
enough to catch a nonexistent column/table/relation before ship. Sketch,
not a decision: collect SQL literals passed to `conn.execute(...)` in
`src/precis_web/**`, and `PREPARE`/`EXPLAIN` each against the gate's test
schema with placeholder params.

## Explicitly NOT in scope

- Asserting query *results* — that stays the job of a `test_*_sql.py`.
- Migrating `precis_web` tests off `FakeStore`. The fake is fast and worth
  keeping for render-level coverage; this closes its one dangerous blind
  spot, it does not replace it.
- SQL outside `src/precis_web/` (workers, ingest, store). Worth a look
  once the web pass proves the approach, but the store layer is already
  covered by real-PG tests.

## Acceptance criteria

- A gate-run check fails when a `precis_web` route names a column or table
  that does not exist in the migrated schema.
- Reverting the `a4775105` fix (restoring `r.slug` in `_project_draft`)
  makes that check go red.
- Zero findings against current `main` at the time it lands, or each
  surviving finding is fixed in the same change.

## Target + blast radius

Test/gate infrastructure only — no runtime behaviour change. Reads every
raw SQL literal under `src/precis_web/`, so expect it to surface unrelated
pre-existing offenders on first run.

## Open questions / decisions log

- How to reach the SQL: AST-walk for `conn.execute` call args (misses
  dynamically composed SQL) vs. runtime capture via a psycopg tracer in
  the fake. AST is simpler and matches the existing `encoding="utf-8"`
  AST-walk test precedent.
- `PREPARE` needs a param count and types; queries built with `psycopg.sql`
  composition or interpolated fragments may not prepare cleanly. Decide
  whether those get an opt-out marker or are simply skipped-and-counted.
