# Claude Code — project brief

> **First**: read `AGENTS.md`. It is the canonical project guide
> (humans + agents). Conventions, workflow, definition-of-done,
> ingest guarantees — all there. This file is a thin pointer with
> two recent-landing notes Claude Code sessions should know about.

## What just landed (2026-05-31)

The **persistent discovery layer** (ADR
`docs/decisions/0018-persistent-discovery-layer.md`). For papers:

- `view='toc'` reads from `ref_segments` + `ref_segment_sentences`
  — no per-request DP + KeyBERT recompute. Worker:
  `precis worker --only segments`.
- Search results carry indented `excerpt @ ~N: "..."` sub-lines
  drawn from `ref_segment_sentences`, **reranked against the query
  embedding via pgvector cosine**. Triage discipline lives in
  `precis-search-help` (`get(kind='skill', id='precis-search-help')`).
- New `citation` kind for the verifier-workflow:
  `put(kind='citation', text=<claim>, source_handle, source_quote,
  verifier_confidence, link='paper:<slug>', rel='cites')`.
  See `precis-citation-help`.
- `chunks.numerics TEXT[]` lexical index — `WHERE numerics @>
  ARRAY['1.523 eV']` for exact quantitative lookups.
- pysbd-backed sentence splitter in the chunker fallback chain.
  Abbreviation-aware (`et al.`, `Fig.`, `i.e.`, `e.g.`, `vs.`).
- Dehyphenation in `marker._clean_text` (joins `-\n` when both
  sides are lowercase ASCII).

## Where to find context

| Task                             | Read |
|----------------------------------|------|
| Workflow + lint/test commands    | `AGENTS.md` |
| Full schema (prose)              | `docs/design/storage-v2.md` |
| Full schema (visual)             | `docs/design/schema-v2.svg` (PUML in same dir) |
| Discovery-layer design rationale | `docs/decisions/0018-persistent-discovery-layer.md` |
| Worker queue pattern             | `docs/decisions/0007-derived-queue-no-block-jobs.md`, `0017` |
| Agent-runtime surface (skills)   | `src/precis/data/skills/precis-*.md` |
| Migrations                       | `src/precis/migrations/0001_initial.sql` is sealed; `0005`–`0007` are the discovery layer |
| Ingest pipeline                  | `src/precis/ingest/{marker,pipeline,text_chunker,db_writer}.py` |
| Worker code                      | `src/precis/workers/{embed,summarize,segment_toc}.py` |

## Conventions that bite

- **Forward-only migrations.** Never edit a sealed `*.sql` file.
  See `docs/decisions/0005-greenfield-migrations.md`.
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

## Recent unreleased changes

See the top of `CHANGELOG.md` under `## Unreleased` for the full
list. The discovery-layer entry is the headline; everything else
folds into it.
