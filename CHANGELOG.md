# Changelog

All entries pre-1.0 are unreleased; v2 is in active development on the
`v2` branch and not yet on PyPI.

## Phase 3 — Paper kind + bundle ingest

End-to-end paper handling: ingest from `.acatome` bundles, hybrid block
search, citation views, CLI cutover commands. 216 tests green.

- `utils/slug.py`: deterministic `<surname><year><word>` minter with
  collision suffixing; pure logic, no DB
- `embedder.py`: `Embedder` Protocol, `MockEmbedder` (deterministic,
  used by all unit tests), shell `BgeM3Embedder` for the optional
  sentence-transformers backend
- `Store` block CRUD: `insert_blocks`, `get_block`,
  `list_blocks_for_ref`, `count_blocks`, `update_block_density`,
  `update_block_embedding`, `blocks_missing_embeddings`
- `Store` block search: `search_blocks_lexical` (tsvector +
  `ts_rank_cd`), `search_blocks_semantic` (pgvector cosine),
  `search_blocks_fused` (RRF, k=60, falls back to lex-only when no
  query vector supplied)
- `ingest.py`: bundle parsing, density classifier, embedding fill,
  slug minting glue
- `Store.ingest_bundle()`: idempotent on DOI; reuses bundle vectors
  when dim matches, re-embeds otherwise; applies `SRC:bundle` tag and
  density tags per block; one transaction per bundle
- `PaperHandler`: slug-addressed read-only kind. `get(id=slug)`
  overview, `id=slug~N` / `id=slug~N..M` chunk selectors,
  `id=slug/cite/bib`/`/abstract`/`/toc` view paths, `view='bibtex'`
  /`'ris'`/`'endnote'`/`'abstract'`/`'toc'` kwargs.
  `search(q=…, kind='paper', scope=slug)` block-level RRF search
- CLI: `precis migrate [--dry-run] [--database-url …]`,
  `precis jobs ingest-bundle <file>`,
  `precis jobs ingest-bundles <dir> [--dry-run] [--limit N]`
- `docs/v2-cutover.md`: ops runbook for the v1 → v2 switch

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
