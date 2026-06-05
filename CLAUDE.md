# Claude Code — project brief

> **First**: read `AGENTS.md`. It is the canonical project guide
> (humans + agents). Conventions, workflow, definition-of-done,
> ingest guarantees — all there. This file is a thin pointer with
> recent-landing notes Claude Code sessions need before touching the
> discovery / search / chase paths. Update it the same commit you
> change the things it describes.

## What just landed (2026-06-05)

**F20: per-chunk KeyBERT supersedes the persistent discovery layer.**
The `ref_segments` / `ref_segment_sentences` tables described in ADR
`0018-persistent-discovery-layer.md` were dropped in migration
`0011_chunk_keywords.sql`. The discovery surface is now:

- `chunks.keywords TEXT[]` (canonical lower-case forms, GIN-indexed)
  + `chunks.keywords_meta JSONB` (versioned envelope with short/long
  pairs and KeyBERT scores).
- Worker: `precis worker --only chunk_keywords` (or run as part of
  the default round-robin). Source:
  `src/precis/workers/chunk_keywords.py`. Claim shape is
  `keywords IS NULL OR keywords_meta->>'version' != current`, so
  bumping `KEYWORDS_VERSION` re-claims every existing chunk.
- `view='toc'` (papers): dynamic DP clustering over the keyword
  arrays at request time — `src/precis/utils/toc_db.py`
  `render_from_store`. No precomputed segment rows; reads
  `chunks.keywords` directly.
- `view='toc'` (skills): still uses the per-request DP+KeyBERT
  renderer in `src/precis/utils/toc.py`; output is memoised per
  `(slug, scope)` on the handler instance since skill files are
  static for the life of the process.
- Search reranking against `ref_segment_sentences` was removed with
  F20. Result rows no longer carry indented `excerpt @ ~N` sub-lines.

**Other live affordances (still current as of 2026-06-05):**

- `citation` kind — verifier-workflow ref kind.
  `put(kind='citation', text=<claim>, source_handle, source_quote,
  verifier_confidence, link='paper:<slug>', rel='cites')`. Skill:
  `precis-citation-help`.
- `chunks.numerics TEXT[]` GIN-indexed lexical filter —
  `WHERE numerics @> ARRAY['1.523 eV']` for exact quantitative
  lookups (migration `0006_chunk_numerics.sql`). Currently
  unwired into the search verbs; available via direct SQL only.
- pysbd-backed sentence splitter in the chunker fallback chain
  (`et al.`, `Fig.`, `i.e.`, `e.g.`, `vs.`-aware).
- Dehyphenation in `marker._clean_text` (joins `-\n` when both
  sides are lowercase ASCII).
- HNSW index on `chunk_embeddings.vector` (migration `0016`)
  — semantic search no longer seq-scans.
- SSRF guard on outbound HTTP (`src/precis/utils/safe_fetch.py`)
  used by `handlers/web.py` and `workers/fetch_oa.py`. DNS-resolves
  the host before fetch and revalidates every redirect against the
  private/loopback/link-local/cloud-metadata blocklist.

## Where to find context

| Task                             | Read |
|----------------------------------|------|
| Workflow + lint/test commands    | `AGENTS.md` |
| Full schema (prose)              | `docs/design/storage-v2.md` |
| Full schema (visual)             | `docs/design/schema-v2.svg` (PUML in same dir) |
| Worker queue pattern             | `docs/decisions/0007-derived-queue-no-block-jobs.md`, `0017` |
| F20 (per-chunk keybert)          | `src/precis/workers/chunk_keywords.py` header + `src/precis/utils/toc_db.py` header |
| ADR 0018                         | Superseded by F20. Tables dropped in `0011_chunk_keywords.sql`. Keep for history, do not implement against. |
| Agent-runtime surface (skills)   | `src/precis/data/skills/precis-*.md` |
| Migrations                       | `0001_initial.sql` is sealed; head is `0016_chunk_embeddings_hnsw.sql`. ADR 0005 governs forward-only discipline. |
| Ingest pipeline                  | `src/precis/ingest/{marker,pipeline,text_chunker,db_writer}.py` |
| Worker code                      | `src/precis/workers/{embed,summarize,chunk_keywords,chase,fetch_oa,runner}.py` |
| SSRF guard                       | `src/precis/utils/safe_fetch.py` |

## Conventions that bite

- **Forward-only migrations.** Never edit a sealed `*.sql` file.
  See `docs/decisions/0005-greenfield-migrations.md`. If you find a
  bug in a sealed file, ship a new forward migration that corrects
  it; do not rewrite history.
- **`uv` for everything.** Bare `pip` / `pytest` / `mypy` are
  not reproducible. Use `scripts/dev pytest …` inside the
  container, or `uv run …` on the host.
- **Container-first ops.** `scripts/dev` → dev shell;
  `scripts/db` → psql. Compose file lives outside the repo at
  `~/work/infrastructure/compose.yaml`.
- **Skills are runtime docs.** Updating a skill file under
  `src/precis/data/skills/` is the agent-facing channel — the
  MCP server reads them at boot and serves them via
  `get(kind='skill', id='…')`.
- **Embeddings populated by the worker, not at ingest.** Per ADR
  0007: ingest stores chunks with `embedding IS NULL`; the
  `embed:bge-m3` worker picks them up. Callers must not call
  `fill_embeddings` from the ingest path.
- **Outbound HTTP goes through `safe_fetch`.** Any new code that
  fetches an agent-supplied URL — directly or after a redirect —
  must use `safe_get` / `safe_stream` from
  `src/precis/utils/safe_fetch.py`. Raw `httpx.Client(...).get(url)`
  with `follow_redirects=True` is an SSRF.

## Recent unreleased changes

See the top of `CHANGELOG.md` under `## Unreleased` for the full
list. F20 (per-chunk keybert) is the headline since 2026-06-05;
everything else folds into it.
