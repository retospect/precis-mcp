# precis-mcp v2

> **Status: pre-alpha, in progress.** This branch (`v2`) is a ground-up rewrite
> of precis-mcp. Earlier (v1) source lives in the `main` branch of
> `retospect/precis-mcp` and locally at `../precis-mcp` (also wired as the
> `v1-local` git remote here).
>
> Phases done: **1 (walking skeleton)**, **2 (DB backbone + memory)**,
> **3 (paper kind + bundle ingest)** — see `docs/v2-cutover.md`,
> **3.5 (navigation parity: hierarchical TOC, drill-down, Next: trailers)**,
> **4a (cache-backed kinds: `math`, `youtube`, `web` page-fetch)**,
> **4b (Perplexity Sonar trio: `websearch` / `think` / `research`)**,
> **5 (state kinds: `todo`, `gripe`, `fc`, `quest`, `conv`, `oracle`, `skill`)**,
> **6a (markdown file handler with read/write + lazy re-ingest)**,
> **7 (precis-help meta-skill synthesised from live registry)**,
> **8 (anchored edit protocol — `mode='edit'` + `mode='insert'` on markdown + python)**,
> **9 (python code-navigator kind — AST index, callgraph, runtrace)**.
> Queued: web bookmark mode + Wayback (deferred), other file handlers (plaintext, rmk, docx, tex, book), polish.

## What v2 is

A Model Context Protocol (MCP) server that exposes a small, uniform API for
agents to read, write, and search across:

- Research papers (PDF → indexed chunks, citations, semantic search)
- Long-form documents (DOCX, LaTeX, Markdown)
- Personal state (todos, memories, gripes, flashcards, conversations)
- Cached paid-tool calls (web search, math, YouTube transcripts)
- Local computations (calc, plot, RNG)

All under **four verbs** (`get`, `search`, `put`, `move`) and a single
`kind=` discriminator. No URI selector strings; everything is keyword args.

## What v2 changes from v1

This is a redesign, not a refactor. Highlights:

- `type=` → `kind=`
- View / subview selectors flattened into kwargs
- Numeric IDs for ephemeral kinds (`todo`, `memory`, `gripe`, `fc`)
- First-class link relations (`related-to`, `blocks`, `contradicts`)
- First-class tag namespaces (`closed`, `flag`, `open`)
- Cache freshness derived from `cache_state` table, not from tags
- HintBus collector — any layer can emit deduped, novelty-decayed hints
- Slim exception hierarchy carrying one `next=` "breaking hint"
- `psycopg 3` (sync) + raw SQL throughout; no SQLAlchemy. Sync below FastMCP
  because async was buying nothing for stdio's serial workload.
- Forward-only numbered SQL migrations; no Alembic
- Hybrid search (lexical tsvector + semantic pgvector, RRF fused)
- Drops entry-point plugin discovery in favour of an in-tree `BUILTINS` list
- Inlines what was `acatome-store`; depends on `acatome-extract` for PDF→bundle

See `docs/store_sketch.py` for the Python store interface, `docs/paper_ingest.md`
for the bundle ingest path, and `src/precis/migrations/0001_initial.sql` for
the schema.

## Status

- [x] Schema designed (`0001_initial.sql`)
- [x] Store interface sketched (`docs/store_sketch.py`)
- [x] Paper ingest spec (`docs/paper_ingest.md`)
- [x] Phase 1 — walking skeleton: four verbs + `calc` end-to-end (no DB)
- [x] Phase 2 — DB backbone: migration runner + `memory` handler
- [x] Phase 3 — `paper` kind: get/search, RRF block search, bundle ingest, `precis migrate` + `precis jobs ingest-bundle(s)` (see `docs/v2-cutover.md`)
- [x] Phase 3.5 — navigation parity: hierarchical TOC, range-scoped drill-down (`~A..B/toc`), column-aligned `Next:` trailers on overview/chunk/toc views — *plan: `docs/phase3.5-plan.md`*
- [x] Phase 4a — cache-backed kinds: `math` (Wolfram), `youtube` (transcripts), `web` (page fetch + trafilatura). Shared `CacheBackedHandler` base, `cache_state` CRUD, attribution footers + cost trailers — *plan: `docs/phase4-plan.md`*
- [x] Phase 4b — Perplexity Sonar trio: `websearch`, `think`, `research`. Shared base, per-tier model + TTL + cost; cache key includes model so tiers don't collide. Web bookmark mode + Wayback enrichment deferred (needs `put` on `web` kind).
- [x] Phase 5 — state kinds: `todo` (with STATUS transitions and `/open` `/done` filters), `gripe`, `fc` (with `/due` view), `quest` (slug-addressed with auto-mint), `conv` (read-only with `/transcript` and per-turn nav), `oracle`, `skill` (markdown served from package data dir). Shared `NumericRefHandler` base extracted from MemoryHandler.
- [x] Phase 6a — `markdown` file handler. Slug-addressed (`notes/meeting.md` → `notes--meeting`); one block per heading / paragraph / fenced code / table / list. Block slugs are content-derived hashes so they survive re-ingest. Lazy re-ingest on every `get` checks mtime first, falls back to sha256 on stale mtime. `put` modes: `create`, `append`, `replace`, `delete` — all atomic. CLI: `precis jobs ingest-md <root>`. See `docs/phase6-plan.md`.
- [x] Phase 7 — `precis-help` meta-skill synthesised from the live registry. `SkillHandler.bind_registry()` is invoked by `build_runtime()` after `Registry` construction; the help skill enumerates every active kind with verbs + description and includes a banner when a documented-but-not-wired kind is requested.
- [x] Phase 8 — anchored edit protocol. New write modes `mode='edit'` and `mode='insert'` join `create`/`append`/`replace`/`delete` on every R/W file kind. Resolves by *content* (literal `find=` with optional `before=`/`after=` anchors and `match='unique|first|all|nth'` policy). Pure resolver in `precis.utils.edit_resolve`; sharp `BadInput` on not-found (with fuzzy nearest-line hints) and ambiguous (with disambiguation guidance). v1 ships for `markdown` and `python`. See `docs/edit-protocol-spec.md`.
- [x] Phase 9 — `python` code-navigator kind. Multi-root, AST-indexed in-memory with mtime-invalidated cache (no DB persistence). Two-track addressing: line ranges (`alias/path/file.py~L42-58`) and qualnames (`alias::pkg.mod.Class.method`). Views: `toc`, `outline`, `source`, `entries` (pyproject console scripts + `__main__` guards), `callgraph` (entry-rooted static call tree with cycle detection + cross-repo resolution), `runtrace` (dynamic call graph captured under `sys.setprofile` in a gated subprocess, with stdlib subtree collapse and a static-only diff). Write surface: same modes as markdown plus three validation gates (`ast.parse`, qualname-drop prevention, `ruff check --fix && ruff format`). Configured via `PRECIS_PYTHON_ROOTS=alias:/path,…`; runtrace gated by `PRECIS_PYTHON_ALLOW_EXEC=1`. See `docs/python-kind-spec.md` and `precis-python-help` skill.
- [ ] Phase 6b — remaining file handlers (`plaintext`, `rmk`, `docx`, `tex`, `book`)
- [ ] Polish: hint channel notifications, cost footer parity across kinds, web bookmark mode + Wayback enrichment

## License

GPL-3.0-or-later.
