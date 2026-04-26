# precis-mcp v2

> **Status: pre-alpha, in progress.** This branch (`v2`) is a ground-up rewrite
> of precis-mcp. Earlier (v1) source lives in the `main` branch of
> `retospect/precis-mcp` and locally at `../precis-mcp` (also wired as the
> `v1-local` git remote here).
>
> Phases done: **1 (walking skeleton)**, **2 (DB backbone + memory)**,
> **3 (paper kind + bundle ingest)** — see `docs/v2-cutover.md`.
> Next: **4 (cache-backed kinds: `web`, `youtube`, `math`)**.

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
- [ ] Phase 4 — cache-backed kinds (`web`, `youtube`, `math`)
- [ ] Phase 5 — remaining state kinds (`todo`, `gripe`, `fc`, `conv`, `quest`, `oracle`, `skill`)
- [ ] Phase 6 — file handlers (`docx`, `tex`, `markdown`, `book`, `plaintext`, `rmk`)
- [ ] Phase 7 — polish: `/help`, hint channel, notifications, cost footer

## License

GPL-3.0-or-later.
