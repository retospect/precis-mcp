# Changelog

All entries pre-1.0 are unreleased; v2 is in active development on the
`v2` branch and not yet on PyPI.

## Perplexity import (`put(mode='import')`)

Pro subscribers can run deep research in the Perplexity web UI for
free, paste the result into precis, and have it land in the *same*
cache row a paid `get` would have produced. Future `get` on the same
query then returns the imported body for $0.
**555 → 565 tests green, 1 skip.**

- `_PerplexityBase.put(id=<query>, text=<report>, mode='import')` —
  validates inputs, parses the body via the existing `parse_markdown`
  splitter (so reports become per-heading / per-paragraph / per-list
  blocks with stable content-derived slugs), embeds the blocks via the
  active `Embedder` if one is configured, and calls
  `Store.put_cache_entry(provider='perplexity', cost_usd=0,
  ttl_seconds=None)`.
- The cache key matches what `get` would compute (`<model>:<query>`)
  so the import populates the row a future paid call would hit. Both
  `refs.meta.source` and `cache_state.meta.source` are set to
  `"imported"` for provenance.
- `WebsearchHandler` / `ThinkHandler` / `ResearchHandler` flip
  `supports_put=True` and advertise `modes=("import",)`. Each kind
  imports under its own model — so the same `id=` imported under
  `research` and `websearch` lives in two distinct cache rows by
  design.
- `_PerplexityBase.__init__` accepts an optional `embedder=`; the
  registry passes the active embedder through. With no embedder
  configured (e.g. stateless test runs) imports still land but
  without per-block vectors.
- New skill: `precis-perplexity-help` — documents the three Sonar
  tiers and the import flow side by side.
- 10 new tests in `tests/test_perplexity.py` cover: import → cache
  hit at $0; multi-block parsing; import without embedder; idempotent
  re-import (replace, not duplicate); per-kind cache isolation;
  `meta.source='imported'` provenance; mode/id/text validation;
  imported blocks are findable via fused block search.
- No DB schema change. No new kinds. No new env vars. No CLI changes.

## Phase 6a — Markdown file handler

The first file-backed kind. Read and edit `.md` files under a
configured root with the same four verbs every other kind uses.
**450 → 520 tests green, 1 skip.**

- `precis.utils.md_parse` — pure-logic markdown splitter. Recognizes
  ATX headings (1–6), fenced code (` ``` ` / `~~~`), pipe tables
  (with separator row), ordered + unordered lists, paragraphs.
  Thematic breaks are dropped; blank lines separate blocks.
  Per-block slugs are content-derived: heading slugs from the
  heading title (`# Hello World` → `hello-world`), other-kind slugs
  from `<5 leading words>-<6 hex>`. Stable across re-ingest.
- `precis.utils.md_parse.file_slug_from_path` — encodes a relative
  file path as a ref slug (`notes/meeting.md` → `notes--meeting`).
  `--` is the segment separator; segments are normalized to
  lowercase a–z 0–9 `_` `-`. `is_valid_file_slug` enforces this on
  every call (defence-in-depth against path traversal even though
  the handler also resolves+checks against the configured root).
- Migration `0004_file_kinds.sql` registers `markdown`, `plaintext`,
  `rmk`, `docx`, `tex` in the `kinds` table (only `markdown` has a
  handler in this session — others queue for phase 6b).
- `precis.config.PrecisConfig.markdown_root` (env:
  `PRECIS_MARKDOWN_ROOT`). The handler is hidden when unset.
- `MarkdownHandler` (slug-addressed, supports get/search/put):
  - `get(id='slug')` — overview + flat heading list (H1 + H2)
    + `Next:` hint trailer.
  - `get(id='slug~SLUG')` — one block by stable slug.
  - `get(id='slug~N')` — one block by 0-indexed pos.
  - `get(id='slug/toc')` — full hierarchical TOC (reuses
    `_paper_toc.build_toc` + `render_toc`).
  - `get(id='slug/raw')` — full source text.
  - `get()` / `get(id='/')` — index of every `.md` file under root.
  - `search(q='...', scope='slug')` — block-level fused-search
    (lexical + vector if embedder).
  - `put(mode='create', id='slug', text=...)` — create new file.
  - `put(mode='append', id='slug', text=...)` — append paragraph.
  - `put(mode='replace', id='slug~SLUG', text=...)` — rewrite one
    block in place.
  - `put(mode='delete', id='slug~SLUG')` — drop one block.
- **Lazy re-ingest**: every `get` first stats the file. If
  `meta.mtime_ns` matches, the cached blocks are served. If mtime
  differs but sha256 matches, only meta is bumped. If sha256
  differs, the file is re-parsed and blocks are atomically replaced.
  Block slugs survive across re-ingest (content-derived). Deleted
  files trigger soft-delete of the ref so the index stays clean.
- **Atomic writes**: every put writes via tmpfile + `os.replace`.
  After write the handler force-re-ingests so the next get sees
  the new state.
- **Path-traversal safety**: ref slugs are validated by
  `is_valid_file_slug`; the resolved path is checked to be under
  the configured root with `Path.relative_to`.
- CLI: `precis jobs ingest-md <root> [--force]` — pre-warm a
  directory (the handler ingests lazily on first `get` anyway, but
  pre-warming is useful before launching long-running searches).
- 70 new tests across 2 files: `test_md_parse.py` (37) covers
  the parser + slug helpers; `test_markdown_handler.py` (33)
  covers handler get/search/put/lazy-reingest end-to-end.
- Skill: `precis-markdown-help.md` documents address shapes,
  block kinds, put modes, CLI usage, and limits.
- Live verification: created `/tmp/precis-md-demo/` with two files,
  ingested, walked the TOC, edited a block via `put(mode=replace)`,
  appended a paragraph, created a new file. All atomic, all
  reflected on next `get`.

## Phase 5 — State kinds (todo, gripe, fc, quest, conv, oracle, skill)

The bulk of the agent-facing API for personal state. Six new kinds
plus the shared base that finally makes adding a new ref kind trivial.
**447 tests green, 1 skip.**

- `precis.handlers._numeric_ref.NumericRefHandler` — extracts the
  shared CRUD shape (get / search / put-create / put-update /
  delete / list-recent) that MemoryHandler had grown organically.
  Subclass contract is tiny: `spec`, `kind`, `sense`,
  `default_tags_on_create`, optional `_render_one` /
  `_render_search_hit` / `_list_view` / `_render_create_ack`.
- `precis.handlers.memory` — refactored to a 30-line subclass of
  the new base. All 20 memory tests still green.
- `TodoHandler` — STATUS:open default-on-create; status transitions
  via closed-prefix tag replacement (STATUS:doing supersedes
  STATUS:open atomically); `/open`, `/doing`, `/blocked`, `/done`,
  `/queue` list views; aligned Next: trailers on every view.
- `GripeHandler` — minimal numeric-ref kind. No default tags,
  free-form body. Lexical search.
- `FlashcardHandler` (`fc`) — knowledge statements with SM-2 review
  state in `ref.meta`. `/due` view surfaces cards whose
  `next_review` is in the past plus an "upcoming within 3 days"
  block. The actual SM-2 grader is deferred until the review-feedback
  agent surface lands.
- `QuestHandler` — slug-addressed work-queue kind with auto-mint:
  `put(text=...)` derives a slug via `slug_from_text`, appends
  `-2`/`-3` on collision. Same STATUS: vocabulary as todos. `/open`
  / `/doing` / `/blocked` / `/done` filters.
- `ConversationHandler` — read-only durable transcripts; one block
  per turn. Three views: overview (`slug`), full transcript
  (`slug/transcript`), single turn (`slug~N`). Block-level
  fused-search via `slug` scope.
- `OracleHandler` — slug-addressed authoritative reference nodes
  (e.g. saved rubrics, prompts). Read-only in phase 5; future `put`
  adds versioning.
- `SkillHandler` — markdown skills served from
  `precis.data.skills` package data via `importlib.resources` (so
  it works from a wheel). `get(kind='skill')` lists every skill with
  its title; `get(kind='skill', id='precis-overview')` returns the
  raw markdown; `search(kind='skill', q='...')` does case-insensitive
  full-text search across all skills. Front-matter `title:` is
  surfaced in the index. Read-only by design — skills are versioned
  with code.
- 51 new tests across 3 files: `test_todo.py` (16), `test_state_kinds.py`
  (24), `test_skill.py` (11).

## Phase 4b — Perplexity Sonar trio

Three new cache-backed kinds sharing one shared base. **396 tests
green, 1 skip.**

- `precis.handlers.perplexity._PerplexityBase` (subclass of
  `CacheBackedHandler`). Subclasses set `model`, `timeout`,
  `cost_per_call_usd`, `ttl_seconds`, and an attribution string.
- `WebsearchHandler` — `sonar`, 30s timeout, 7-day TTL,
  ~$0.001/call.
- `ThinkHandler` — `sonar-reasoning-pro`, 120s, 30-day TTL,
  ~$0.005/call.
- `ResearchHandler` — `sonar-deep-research`, 600s, **pinned**
  cache (these cost ~$0.50 each — never expire automatically),
  ~$0.50/call.
- Cache key is `<model>:<query>` so the same prompt under different
  tiers never collides on the `(provider='perplexity',
  request_hash)` unique index.
- Per-Perplexity-ToS attribution: every response carries a footer
  noting AI generation, model used, citations are not primary
  sources, and ToS disclosure requirements.
- Cache-hit Next: trailer suggests the next tier up
  (websearch → think → research) and a deep-link to fetch the
  first cited URL via `kind='web'` for primary-source verification.
- Migration `0003_perplexity_kinds.sql` registers the three kinds
  in the `kinds` table.
- 23 new tests with mocked httpx + env. All HTTP error cases
  (401/429/5xx/timeout/network) map to the correct `Upstream`
  variants.

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

## Phase 3.5 — Navigation parity

The user-facing navigation that made v1 distinctive, restored. **373
tests green, 1 skip.**

- `precis.utils.next_block` — `format_next_block` and
  `render_next_section` helpers. Column-aligned `(call, description)`
  pairs with em-dash separators; the formatter is shared across all
  handlers that emit `Next:` trailers.
- `precis.handlers._paper_toc` — heading detection (acatome
  `■ **NAME**` / `**Name**` / markdown `# Name` / `## Name`), section
  grouping, range-scoped clipping for drill-down, hierarchical
  rendering. Pure logic; no DB dependency.
- `PaperHandler.get(view='toc')` now produces a structured jump table
  with section/subsection ranges, block counts, indented children, and
  a "Next:" trailer pointing at the largest section to drill into.
  Replaces the flat "block 0 / block 1 / block 2 …" listing.
- `PaperHandler` accepts the combined drill-down id form
  `slug~A..B/toc` — TOC scoped to that range. Recursive: each child
  section is itself addressable.
- Aligned `Next:` trailers added to every PaperHandler view:
  - **overview**: TOC, first chunks, BibTeX, scoped search
  - **chunks**: next/previous range (sized to match the current
    range), full TOC, range-scoped TOC, BibTeX
  - **TOC**: drill into largest section, read largest section, BibTeX
- Live verified against the real `acheson2026automated` paper (177
  blocks → 20 detected sections; METHODS has 4 H2 children; RESULTS &
  DISCUSSION has 2). Drill-down to `~74..116/toc` correctly clips to
  just RESULTS & DISCUSSION + its children.

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
