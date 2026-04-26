# Changelog

All entries pre-1.0 are unreleased; v2 is in active development on the
`v2` branch and not yet on PyPI.

## Phase 2 — DB backbone (sync, psycopg 3) + memory handler

End-to-end ref-backed kind via local postgres. Sync top-to-bottom below
FastMCP. 88 tests green.

- `psycopg[binary,pool]` 3.2; pgvector codec via `pgvector.psycopg`
- `Store` (sync): corpus, ref CRUD, tag CRUD, system settings
- `Migrator`: forward-only SQL migrations with sha256 checksum guard
- `MemoryHandler`: first ref-backed kind. Numeric id, get/search/put,
  closed-prefix tag replacement
- Schema fixes: renamed `symmetric` → `is_symmetric` (postgres reserved
  word); `pos = -1` sentinel for ref-level (PK/UNIQUE without partial
  indexes)
- `tests/conftest.py` ephemeral-DB fixture (no docker, no testcontainers)

## Phase 1 — Walking skeleton (4 verbs + calc + HintBus)

End-to-end MCP server with one stateless kind. No DB. 39 tests green.

- `errors.py`: `PrecisError` hierarchy with `next=` breaking hint
- `hints.py`: `HintBus` contextvar collector, dedup with cooldown ring
- `runtime.py`: `PrecisRuntime` verb dispatch + error rendering
- `server.py`: FastMCP stdio server exposing `get/search/put/move`
- `cli.py`: `precis serve | migrate | jobs`
- `handlers/calc.py`: sympy-backed stateless calculator

## Design artefacts (pre-phase-1)

Ground-up rewrite. v1 history preserved in `main` branch upstream and on
the `v1-local` git remote. Breaking redesign — nothing wire-compatible
with v1.

- Schema: `src/precis/migrations/0001_initial.sql`
- Python store interface sketch: `docs/store_sketch.py`
- Paper-ingest spec: `docs/paper_ingest.md`
- Phase-3 plan: `docs/phase3-plan.md`
