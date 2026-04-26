# Changelog

All entries pre-1.0 are unreleased; v2 is in active development on the
`v2` branch and not yet on PyPI.

## Phase 4a — Cache-backed kinds (math, youtube, web)

Three new kinds plus the shared infrastructure they need. 331 tests
green, 1 skip.

- Migration `0002_cache_providers.sql` adds the `web` provider row
  (others ship in 0001).
- `Store.get_cache_entry(provider, request_hash)` and
  `Store.put_cache_entry(...)` — atomic ref + `cache_state` upsert,
  hard-replaces existing refs with the same kind+slug so re-fetches
  cleanly cascade away stale blocks.
- `CacheBackedHandler` base in `handlers/_cache_base.py`. Shared
  cache flow: hash → lookup → freshness check → fetch-on-miss →
  attribution footer → cost trailer. Subclass contract is small:
  `provider`, `ttl_seconds`, `attribution`, `corpus_slug`,
  `_canonical_key`, `_fetch`. `FetchResult` dataclass wraps the
  upstream result.
- `MathHandler` (Wolfram Alpha): hand-rolled httpx GET to bypass two
  upstream `wolframalpha` library bugs (asyncio.run-in-loop, strict
  Content-Type assertion). Pod → markdown formatter ported from v1.
  Per-query deep-link + paste-ready academic citation appended to
  attribution. Cache pinned (results deterministic).
- `YouTubeHandler`: cache key is the bare 11-char video id, so URL
  variants (youtu.be / watch?v= / shorts / embed / live / mobile)
  collapse onto one row. Language preferences are part of the key
  (en/es cache separately). `view='languages'` side query lists
  available tracks. 30-day TTL.
- `WebHandler`: page-fetch mode. Canonical URL is the cache key
  (drops tracking params, default ports, fragments on non-SPA hosts).
  Article extracted with trafilatura → markdown body. 7-day TTL.
  Phase 4a ships fetch-mode only; bookmark mode + Wayback deferred
  to phase 4b.
- `precis.utils.url` ports v1's URL canonicalization
  (`canonical_url`, `slug_from_url`, `is_http_url`, `host_of`).
- All three kinds wire into the registry behind a try/ImportError
  guard: missing optional dep (`[external]` extra) silently hides
  the kind without breaking server startup.
- Skill drafts: `precis-math-help.md`, `precis-youtube-help.md`,
  `precis-web-help.md`.

## Phase 3.5 plan — Navigation parity

Queued (after phase 4): hierarchical TOC, range-scoped drill-down
(`~46..105/toc`), aligned "Next:" trailer block on every paper view.
The user-facing navigation that made v1 distinctive — see
`docs/phase3.5-plan.md`. ~150 LOC, ~20 tests.

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
