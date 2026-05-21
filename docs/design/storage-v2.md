# storage-v2 — design plan

- **Status**: revised (2026-05-21)
- **Authors**: Reto + agent
- **Related ADRs**:
  - [`0001-merge-acatome-into-precis.md`](../decisions/0001-merge-acatome-into-precis.md)
  - [`0002-pub-id-and-toon.md`](../decisions/0002-pub-id-and-toon.md) (TOON portion still in force; identifier section superseded)
  - [`0005-greenfield-migrations.md`](../decisions/0005-greenfield-migrations.md)
  - [`0006-tri-identifier-scheme.md`](../decisions/0006-tri-identifier-scheme.md)
  - [`0007-derived-queue-no-block-jobs.md`](../decisions/0007-derived-queue-no-block-jobs.md)
- **Sub-plans**: [`pip-merge.md`](./pip-merge.md) (the file-by-file mapping for step B)

## Goals

This plan covers a coordinated set of changes that touch schema,
ingest pipeline, CLI surface, and container layout. Doing them
together avoids three painful migrations.

In scope:

1. **Single image + multi-service compose** (per ADR 0001).
2. **Lazy embeddings + summaries via worker queue** — keep ingest
   fast; embed and summarize asynchronously; survive crashes;
   parallelize across machines.
3. **Multi-embedder schema** — multiple vectors per chunk in
   different models so we can experiment without re-ingesting.
4. **Chunks layer** — chunks are the unit of embedding, derived from
   raw Marker blocks. Better semantic granularity than current
   block-level embeddings.
5. **`precis add` as the primary ingest entry point**, with `watch`
   and `worker` as thin wrappers.
6. **`pub_id` first-class column** (ADR 0002).
7. **Manual-verified flag** on refs.
8. **Canonical content hash** for PDF dedup that survives re-saves.
9. **Multi-paper-per-PDF** support (proceedings, letters sections).
10. **Retraction tracking** for owned and cited papers (schema only;
    populating waits on the citation graph).
11. **Adopt TOON for tabular MCP responses** (ADR 0002).

Out of scope (separate plans):
- Citation graph extraction from PDF reference lists.
- Migration of pre-v2 ref data — we wipe and re-ingest from
  `~/work/new_papers/` and `~/work/corpus/`.
- Content-addressed PDF blob filesystem layout (we'll keep
  slug-based filenames for v2; revisit when corpus exceeds 100 K
  refs).
- Backwards-compat shims for `acatome-extract` / `acatome-store`
  imports.

## Identity & naming (recap)

Per ADR 0006, three user-visible identifiers plus internals:

| Identifier | Where | Format | Audience | Properties |
|---|---|---|---|---|
| `paper_id` | `refs.paper_id` | `arxiv:..` / `doi:..` / `sha256:..` | internal | priority arxiv > doi > sha256 of pdf bytes |
| `pub_id` | `refs.pub_id` | 6-char base32 lowercase | machines (DB, MCP, URLs) | `base32(sha256(paper_id))[:6].lower()`; pinned at first ingest |
| `cite_key` | `refs.cite_key` | `miller23a` (firstauthor + 2-digit year + collision suffix) | LaTeX writers | minted at first ingest with collision-letter suffix |
| `slug` | `refs.slug` | `<surname><year><word>` | human readers | filename, `precis show` output, optional URL alias |
| `ref_id` | `refs.ref_id` | bigserial | internal FK | not surfaced to clients |
| `pdf_sha256` | `pdfs.pdf_sha256` | hex SHA-256 of file bytes | internal | dedup key for binary identity |
| `content_hash` | `pdfs.content_hash` | hex SHA-256 of normalized text | internal | dedup key for "same paper, different bytes" |

`pub_id` is **pinned at first ingest** and never changes. `cite_key`
and `slug` are also immutable for new mints. All three resolve via
`ref_identifiers`. Re-ingest of the same `paper_id` produces the
same `pub_id` (deterministic). See ADR 0006 for the full rationale
and ADR 0002 for the unchanged `pub_id` derivation.

## Schema v2

Per ADR 0005, this is **greenfield**: a single `0001_initial.sql`
replaces the legacy `0001`–`0009` migrations. No layered ALTERs;
every column ships in its final shape from migration zero. The
sections below describe the schema in logical groupings (refs, pdfs,
chunks, embeddings, summaries, queue-via-derived-state, graph). The
actual SQL is one file — see B1 in `pip-merge.md`.

### `refs` table

Carries all three user-visible identifiers (`pub_id`, `cite_key`,
`slug`) plus internals, verification flags, and retraction columns.

```sql
CREATE TABLE refs (
  ref_id              BIGSERIAL PRIMARY KEY,
  -- identifiers (ADR 0006)
  paper_id            TEXT     NOT NULL UNIQUE,    -- arxiv:.. / doi:.. / sha256:..
  pub_id              CHAR(6)  NOT NULL UNIQUE,    -- base32(sha256(paper_id))[:6].lower()
  cite_key            TEXT     NOT NULL UNIQUE,    -- miller23a — bibtex-friendly
  slug                TEXT     NOT NULL UNIQUE,    -- miller2023dopamine — long human form
  -- core metadata
  kind                TEXT     NOT NULL,           -- 'paper', 'note', 'code', 'patent', 'skill', 'decision', …
  title               TEXT     NOT NULL,
  authors             JSONB    NOT NULL,           -- list of {family, given, ...}
  year                INT,
  -- human verification
  human_verified_at   TIMESTAMPTZ,
  human_verified_by   TEXT,
  human_verified_note TEXT,
  -- retraction tracking (populated by worker; cited-paper propagation deferred)
  retraction_status   TEXT
    CHECK (retraction_status IS NULL OR
           retraction_status IN ('retracted', 'corrected', 'expression_of_concern')),
  retracted_at        TIMESTAMPTZ,
  retraction_reason   TEXT,
  retraction_source   TEXT,
  retraction_url      TEXT,
  -- multi-paper-per-PDF
  pdf_sha256          CHAR(64) REFERENCES pdfs(pdf_sha256),
  pdf_pages           INT4RANGE,                   -- NULL = whole PDF; e.g. '[3,8)'
  pdf_role            TEXT,                        -- 'main', 'comment', 'reply', 'letter'
  -- bookkeeping
  meta                JSONB    NOT NULL DEFAULT '{}',
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX refs_kind_idx          ON refs (kind);
CREATE INDEX refs_year_idx          ON refs (year) WHERE year IS NOT NULL;
CREATE INDEX refs_retraction_idx    ON refs (retraction_status) WHERE retraction_status IS NOT NULL;
CREATE INDEX refs_human_verified_idx ON refs (human_verified_at) WHERE human_verified_at IS NOT NULL;
```

### `ref_identifiers` table — alias index

All identifier lookups go through here. A single resolver handles
`pub_id`, `cite_key`, `slug`, DOI, arXiv id, S2 paperId, pdf_sha256,
content_hash — same query, same index, same code path.

```sql
CREATE TABLE ref_identifiers (
  id_kind   TEXT NOT NULL,    -- 'pub_id', 'cite_key', 'slug', 'doi', 'arxiv', 's2', 'pdf_sha256', 'content_hash'
  id_value  TEXT NOT NULL,
  ref_id    BIGINT NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
  source    TEXT,             -- 'crossref', 'semantic_scholar', 'manual', …
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (id_kind, id_value)
);
CREATE INDEX ref_identifiers_ref_id_idx ON ref_identifiers (ref_id);
```

### `pdfs` table — normalized PDF storage

One row per unique PDF, dedup by `pdf_sha256`. Multiple `refs` may
point at the same PDF (proceedings, letters sections) with distinct
`pdf_pages`.

```sql
CREATE TABLE pdfs (
  pdf_sha256   CHAR(64) PRIMARY KEY,           -- SHA-256 of file bytes
  content_hash CHAR(64) NOT NULL,              -- SHA-256 of normalized text
  page_count   INT      NOT NULL,
  size_bytes   BIGINT   NOT NULL,
  storage_path TEXT     NOT NULL,              -- e.g. corpus/s/smith2024foo.pdf
  ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX pdfs_content_hash_idx ON pdfs (content_hash);
```

`refs.pdf_pages` is `INT4RANGE`; `[3, 8)` = pages 3 to 7 inclusive
of start, exclusive of end. NULL = the whole PDF. Refs that have no
PDF (notes, code symbols, oracle Q&A) leave `pdf_sha256` NULL.

### `chunks` layer (kind-agnostic)

Chunks are the unit of embedding and search. Derived from raw
`blocks` for papers, but kind-agnostic: any text-bearing ref
(skill, code symbol, decision, note, oracle Q&A) becomes chunks.
The `chunks` table doesn't know whether its parent ref is a paper.

Key design choices:
- `page_first` / `page_last` are NULL-able — only papers have pages.
- `block_ids` is empty for non-paper refs (no Marker blocks).
- `chunk_kind` enum covers paper structures **and** other ref types.
  Adding a new kind is one line in the CHECK constraint plus a
  follow-up ADR.

```sql
CREATE TABLE chunks (
  chunk_id     BIGSERIAL PRIMARY KEY,
  ref_id       BIGINT NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
  ord          INT    NOT NULL,                  -- ordering within ref
  block_ids    BIGINT[] NOT NULL DEFAULT '{}',   -- Marker blocks (papers only); empty otherwise
  text         TEXT   NOT NULL,
  token_count  INT,                              -- approximate, for budgeting
  section_path TEXT[] NOT NULL DEFAULT '{}',
  page_first   INT,                              -- NULL for non-paper refs
  page_last    INT,                              -- NULL for non-paper refs
  chunk_kind   TEXT   NOT NULL
    CHECK (chunk_kind IN (
      -- paper-structural
      'paragraph', 'figure', 'table', 'equation', 'caption', 'heading',
      -- prose / general
      'body', 'abstract',
      -- non-paper kinds (B12 self-ingest)
      'code_symbol', 'qa_pair', 'skill_section', 'decision_section',
      -- skipped from default search
      'references'
    )),
  meta         JSONB  NOT NULL DEFAULT '{}',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (ref_id, ord)
);
CREATE INDEX chunks_ref_id_idx       ON chunks (ref_id);
CREATE INDEX chunks_chunk_kind_idx   ON chunks (chunk_kind);
CREATE INDEX chunks_section_path_idx ON chunks USING GIN (section_path);
```

### `chunk_embeddings` — many vectors per chunk

Keyed by `(chunk_id, embedder)`. Status columns drive the
derived-queue worker (ADR 0007): a chunk that lacks a row for
embedder X is "pending"; a row with `status='failed'` is a poison
pill that the worker skips.

```sql
CREATE TABLE chunk_embeddings (
  chunk_id    BIGINT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  embedder    TEXT   NOT NULL REFERENCES embedders(name),
  vector      vector,                            -- NULL on failure
  status      TEXT   NOT NULL DEFAULT 'ok'
              CHECK (status IN ('ok', 'failed')),
  attempts    INT    NOT NULL DEFAULT 1,
  last_error  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (chunk_id, embedder)
);
CREATE INDEX chunk_embeddings_failed_idx
  ON chunk_embeddings (chunk_id, embedder)
  WHERE status = 'failed';
-- HNSW indexes per embedder are created on demand, not here.
```

### `chunk_summaries` — many summary kinds per chunk

Same derived-queue shape as `chunk_embeddings`.

```sql
CREATE TABLE chunk_summaries (
  chunk_id    BIGINT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  summarizer  TEXT   NOT NULL,                   -- 'rake' | 'gpt-4-mini' | …
  text        TEXT,                              -- NULL on failure
  prompt_hash CHAR(64),                          -- of the prompt template used
  token_count INT,
  status      TEXT   NOT NULL DEFAULT 'ok'
              CHECK (status IN ('ok', 'failed')),
  attempts    INT    NOT NULL DEFAULT 1,
  last_error  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (chunk_id, summarizer)
);
```

### `embedders` — registry of active embedding models

```sql
CREATE TABLE embedders (
  name       TEXT PRIMARY KEY,                   -- 'bge-m3' (default)
  dim        INT  NOT NULL,
  is_default BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO embedders (name, dim, is_default)
  VALUES ('bge-m3', 1024, TRUE);
```

(There is no legacy `blocks.embedding` to drop — greenfield.)

### Worker queue — derived state, no jobs table

Per ADR 0007, there is **no `block_jobs` table**. The queue is
derived: a chunk needing an embedding is one that lacks a row in
`chunk_embeddings` for the relevant `embedder`. The same shape
applies to summaries.

Worker claim query (per artifact kind):

```sql
-- chunks missing the bge-m3 embedding
BEGIN;
SELECT c.chunk_id, c.text
  FROM chunks c
  LEFT JOIN chunk_embeddings ce
    ON ce.chunk_id = c.chunk_id AND ce.embedder = 'bge-m3'
 WHERE ce.chunk_id IS NULL
 ORDER BY c.chunk_id
 LIMIT 64
   FOR UPDATE OF c SKIP LOCKED;
-- worker computes vector(s), then:
INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status)
  VALUES ($1, 'bge-m3', $2, 'ok');
COMMIT;
```

Failure path: the worker still inserts a row, but with `vector=NULL`,
`status='failed'`, `last_error=...`. The next pass's `LEFT JOIN`
predicate (`ce.chunk_id IS NULL`) filters that row out — failed
chunks are not retried automatically. Manual retry:

```sql
DELETE FROM chunk_embeddings
 WHERE chunk_id = $1 AND embedder = $2 AND status = 'failed';
```

### Ref-level jobs (retraction checks)

For `check_retraction:crossmark` the same shape applies: the
output goes into the `refs.retraction_*` columns. "No row yet" is
encoded as `retraction_status IS NULL AND retraction_checked_at IS
NULL`. We add a `retraction_checked_at` column for that purpose:

```sql
ALTER TABLE refs ADD COLUMN retraction_checked_at TIMESTAMPTZ;
```

(In the greenfield SQL this is a normal column declaration in the
single migration; the `ALTER` shown is for cognitive contrast only.)

## Ingest pipeline (the new flow)

`precis add <input>` is the single entry point. Pipeline:

```
              ┌────────────────────────────────────────────┐
INPUT ───►    │  parse_input()                              │
              │   - PDF: extract metadata, sidecar, hash    │
              │   - --doi/--arxiv: skip extraction          │
              │   - --bibtex: parse                         │
              └────────────────────────────────────────────┘
                              │
                              ▼
              ┌────────────────────────────────────────────┐
              │  resolve_identity()                         │
              │   - lookup S2 / CrossRef / arXiv            │
              │   - compute paper_id, pub_id, slug          │
              │   - dedup via ref_identifiers               │
              └────────────────────────────────────────────┘
                              │
                              ▼
              ┌────────────────────────────────────────────┐
              │  store_pdf()  (if any)                      │
              │   - copy to corpus/<letter>/<slug>.pdf      │
              │   - INSERT INTO pdfs ON CONFLICT DO NOTHING │
              └────────────────────────────────────────────┘
                              │
                              ▼
              ┌────────────────────────────────────────────┐
              │  store_ref()                                │
              │   - INSERT INTO refs                        │
              │   - INSERT INTO ref_identifiers (DOI, arxiv,│
              │       s2, pdf_sha256, content_hash)         │
              └────────────────────────────────────────────┘
                              │
                              ▼
              ┌────────────────────────────────────────────┐
              │  extract_blocks()  (PDF only)               │
              │   - Marker → fitz fallback                  │
              │   - INSERT INTO blocks                      │
              └────────────────────────────────────────────┘
                              │
                              ▼
              ┌────────────────────────────────────────────┐
              │  build_chunks()                             │
              │   - merge adjacent same-section blocks      │
              │   - split at sentence boundaries to budget  │
              │   - INSERT INTO chunks                      │
              └────────────────────────────────────────────┘
                              │
                              ▼
              ┌────────────────────────────────────────────┐
              │  enqueue_jobs()                             │
              │   - 'embed:bge-m3' for each chunk           │
              │   - 'summarize:rake' for each chunk         │
              │   - 'check_retraction:crossmark' for ref    │
              └────────────────────────────────────────────┘
                              │
                              ▼
                          DONE  (returns pub_id)
```

The whole flow runs inside one transaction per `precis add` call so
a crash leaves no half-written ref. The expensive work (embed,
summarize, retraction checks) is queued for workers.

`precis watch <dir>` becomes a thin loop:

```python
def watch(dir_path: Path):
    for pdf in observe(dir_path):
        try:
            pub_id = precis_add(pdf)
            move_to_corpus(pdf, pub_id)        # only on success
        except Exception as e:
            move_to_errors(pdf, e)
```

## Worker daemon

`precis worker` is the queue consumer. One worker process can run
multiple artifact kinds in parallel via threads (each kind gets its
own thread with its own loaded model).

```python
ARTIFACTS = [
    # (name, claim_query, handler)
    ('embed:bge-m3',       claim_chunks_missing_embedding('bge-m3'),     embed_handler('bge-m3')),
    ('summarize:rake',     claim_chunks_missing_summary('rake'),         rake_handler()),
    ('summarize:gpt-4-mini', claim_chunks_missing_summary('gpt-4-mini'), llm_handler('gpt-4-mini')),
    ('check_retraction:crossmark', claim_refs_missing_retraction_check(), crossmark_handler()),
]

def worker(artifacts: list[str], poll_interval: float = 1.0):
    while not stop_event.is_set():
        had_work = False
        for name, claim_q, handler in ARTIFACTS:
            if name not in artifacts: continue
            for row in claim_q(limit=64):
                try:
                    handler(row)               # writes ok row
                except Exception as e:
                    handler.write_failed(row, e)  # writes status='failed' row
                had_work = True
        if not had_work:
            sleep(poll_interval)
```

Operational notes:
- A single `precis worker` process is enough for a laptop deployment.
- Multiple workers across machines: `FOR UPDATE OF chunks SKIP LOCKED`
  prevents two workers from claiming the same chunk. No
  `worker_id` registry needed.
- No automatic retry. Transient failures get a `status='failed'`
  row; an operator deletes it to retry. If transient failures
  become routine, add a `next_attempt_at` column and a backoff
  loop. Defer until measured.
- `precis worker --status` runs an aggregate over the output
  tables and prints `(total | ok | failed | pending)` per
  artifact.

## Chunking strategy

Blocks are immutable raw output from Marker (or fitz fallback).
Chunks are derived; we can re-chunk by dropping the `chunks` table
and re-running.

Chunker rules:

1. **Group adjacent blocks of the same `block_type` and same
   `section_path`** into a single chunk, until the token budget
   (default 400 tokens, max 600) is reached.
2. **Headings stay attached to the chunk that follows them.** If a
   heading is the last block of a chunk, it gets carried over to the
   next chunk.
3. **Figures and tables are their own chunks**, carrying the caption
   as their `text`. The image data lives elsewhere (we keep the
   block reference; image binaries are not in v2 scope).
4. **Equations are merged into the surrounding paragraph** if short
   (< 50 tokens), otherwise kept as their own chunk with
   `chunk_kind='equation'`.
5. **References / bibliography sections** are skipped from default
   embedding (`chunk_kind='references'` and `embed:bge-m3` is *not*
   enqueued). They land in chunks but are filtered out of search.

The chunker is deterministic and pure-Python — no LLM. Re-running
yields identical chunks.

## Manual verification

`refs.human_verified_at`, `refs.human_verified_by` set by:

```bash
precis verify <pub_id> --by reto
precis verify <pub_id> --unset                # clear flag
precis verify <pub_id> --note "checked title against PDF cover page"
```

Search and citation surfaces include a verification badge: papers
verified by a human appear with `[a3f7k1 ✓]` in TOON output.

## Multi-paper PDFs

Default flow: one PDF, one paper. Add flags for the multi-paper case:

```bash
# Manual: define each paper's page range explicitly
precis add proceedings.pdf --doi 10.1145/3567 --pages 1-8
precis add proceedings.pdf --doi 10.1145/3568 --pages 9-12

# Auto-detect (best-effort scan for DOI patterns + page-break boundaries)
precis add proceedings.pdf --auto-split
# → opens an editor with proposed splits for confirmation
```

The PDF is stored once in `pdfs`; multiple `refs` rows reference it
with distinct `pdf_pages`. Block extraction runs once over the full
PDF; chunk creation filters blocks by `pdf_pages` per ref.

## Retractions

**Schema** lands in `0010_pub_id_and_verification.sql` (above).

**Worker job kinds:**

- `check_retraction:crossmark` — call the Crossmark API for the DOI;
  parse the `update-to` field; populate retraction columns.
- `check_retraction:retractionwatch` — query the Retraction Watch
  open dataset; populate.

Both are enqueued at ingest time and re-enqueued by a daily scheduler
(out of scope for v2 — manual cron for now).

**Citation-graph retraction propagation** is not implemented in v2
(needs `links` populated). The schema permits it: a future query
joins `refs` with `links` to surface "papers I cite that have been
retracted".

## TOON output

A new module `precis.format.toon` ships with:

```python
def dump(data: dict | list, *, sep: str = "\t", schema: list[str] | None = None) -> str: ...
def load(text: str) -> dict | list: ...
```

A serializer registry (`precis.format.SERIALIZERS`) lets us add JSON
later as a one-liner without conditionals in callers. Default
remains TOON.

CLI behavior:
- TTY: `rich`-rendered table.
- Pipe / redirect: TOON.
- `--format toon|table` flag for explicit override.
- No `--format json` in v2; add later if a real user need surfaces.

## CLI surface

Replaces / consolidates `acatome-extract`, `acatome ...`, and the
existing `precis ingest-bundle*` commands.

```
precis add <pdf|--doi|--arxiv|--bibtex|--interactive>
                         # primary entry point
precis watch <dir>       # daemon: directory watch
precis worker            # queue consumer
precis serve             # MCP server (existing)
precis search <query>    # MCP-equivalent CLI search
precis show <handle>     # ref detail (handle = pub_id | slug)
precis verify <handle>   # toggle human_verified_at
precis health <handle>   # diagnostics: retractions, broken links, …
precis migrate           # SQL migrations (existing)
precis stats             # row counts, queue depths
```

The seven-verb MCP surface (`list`, `get`, `search`, `put`,
`edit`, `delete`, `cite`) is unchanged.

## Compose / image strategy

Per ADR 0001:

```yaml
# infrastructure/compose.yaml
x-platform: &platform
  image: precis-platform:latest
  build:
    context: ..
    dockerfile: infrastructure/Dockerfile
  environment:
    PRECIS_DATABASE_URL: ""
  volumes:
    - ${HOME}/.secrets/pw:/secrets:ro
    - precis-cache:/home/precis/.cache
  extra_hosts:
    - host.docker.internal:host-gateway

services:
  precis-watch:
    <<: *platform
    container_name: precis-watch
    command: ["precis", "watch", "/inbox"]
    volumes:
      - ${HOME}/.secrets/pw:/secrets:ro
      - precis-cache:/home/precis/.cache
      - ${HOME}/work/new_papers:/inbox:rw
      - ${HOME}/work/corpus:/corpus:rw

  precis-worker:
    <<: *platform
    container_name: precis-worker
    command: ["precis", "worker"]

  precis-mcp:
    <<: *platform
    container_name: precis-mcp
    command: ["precis", "serve", "--port", "8765"]
    ports:
      - "8765:8765"

volumes:
  precis-cache:
    driver: local
```

One image (`precis-platform`), three containers, shared cache
volume for model files.

## Implementation order

Greenfield revision: schema is one file, code merge happens
incrementally. The B-step naming matches `pip-merge.md` so commit
messages and the plan stay in sync.

- **A** Workflow scaffolding — AGENTS.md, conventions, ADRs
  0001/0002/0005/0006/0007, this design doc, `pip-merge.md`. ✅ done
- **B0** Sub-plan + ADR 0005 (greenfield migrations). ✅ done
- **B1** Greenfield `0001_initial.sql` — single SQL file with the
  whole v2 schema. Replaces `0001`–`0009`.
- **B2** `precis.identity` module — `make_paper_id`,
  `make_pub_id`, `make_cite_key`, `make_slug`, `make_node_id`,
  `make_pdf_hash`, `make_content_hash`. Pure functions, fully
  unit-tested.
- **B3** Vendor `precis.ingest.*` from `acatome-extract` and
  `acatome-meta`. Imports adjusted; legacy bundle ingest path
  still works.
- **B4** `precis add` CLI — direct DB writes, no `.acatome` file.
- **B5** `precis watch` CLI — calls `precis_add()` directly.
- **B6** `precis worker` — derived-queue consumer (ADR 0007).
- **B7** Drop legacy bundle ingest (`Store.ingest_bundle`,
  `precis ingest-bundle*`).
- **B8** `pyproject.toml` cleanup — drop `acatome-extract`, add
  direct deps (`marker-pdf`, `httpx`, …).
- **B9** Compose update (in `infrastructure/`) — rename
  `acatome-watch` to `precis-watch`.
- **B10** TOON output (`precis.format.toon`).
- **B11** Cutover — apply `0001_initial.sql` to a fresh DB,
  archive old corpus, re-ingest via `precis watch`.
- **B12** *(deferred)* `precis ingest-self` — walk
  `src/precis/data/skills/`, `src/precis/`, `docs/decisions/`,
  `docs/design/` and ingest as `kind in (skill, code_symbol,
  decision, design)`. Re-review the `chunks.chunk_kind` enum
  before this lands.

Each step ships its own commit with tests. Schema-touching steps
run `precis migrate --dry-run` against a fresh DB.

## Schema diagram (Mermaid ER)

```mermaid
erDiagram
    refs ||--o{ ref_identifiers : "aliases"
    refs }o--o| pdfs : "pdf_sha256 (NULL for non-paper kinds)"
    refs ||--o{ chunks : "1..N chunks"
    refs ||--o{ ref_tags : "tagging"
    refs ||--o{ links : "src/dst"
    chunks ||--o{ chunk_embeddings : "by embedder"
    chunks ||--o{ chunk_summaries : "by summarizer"
    embedders ||--o{ chunk_embeddings : "registry"
    tags ||--o{ ref_tags : "applied"
    refs {
        bigserial ref_id PK
        text paper_id UK
        char pub_id UK "6-char base32"
        text cite_key UK "miller23a"
        text slug UK "miller2023dopamine"
        text kind "paper, note, code, skill, ..."
        text title
        jsonb authors
        int year
        timestamptz human_verified_at
        text retraction_status
        char pdf_sha256 FK "NULL for non-paper"
        int4range pdf_pages
        text pdf_role
    }
    ref_identifiers {
        text id_kind PK "pub_id, cite_key, doi, arxiv, ..."
        text id_value PK
        bigint ref_id FK
    }
    pdfs {
        char pdf_sha256 PK
        char content_hash
        int page_count
        bigint size_bytes
        text storage_path
    }
    chunks {
        bigserial chunk_id PK
        bigint ref_id FK
        int ord
        text text
        text chunk_kind "paragraph, figure, code_symbol, ..."
        int page_first "NULL for non-paper"
        int page_last
        text_array section_path
    }
    chunk_embeddings {
        bigint chunk_id PK_FK
        text embedder PK_FK
        vector vector "NULL on failure"
        text status "ok, failed"
        int attempts
        text last_error
    }
    chunk_summaries {
        bigint chunk_id PK_FK
        text summarizer PK
        text text "NULL on failure"
        text status "ok, failed"
        int attempts
        text last_error
    }
    embedders {
        text name PK "bge-m3"
        int dim
        boolean is_default
    }
    tags {
        bigserial tag_id PK
        text tag_kind
        text tag_value
    }
    ref_tags {
        bigint ref_id FK
        bigint tag_id FK
    }
    links {
        bigserial link_id PK
        bigint src_ref_id FK
        bigint dst_ref_id FK
        text relation
    }
```

Notation: `||--o{` = one-to-many; `}o--o|` = many-to-zero-or-one;
`PK` = primary key; `FK` = foreign key; `UK` = unique. The
diagram lives in this design doc and gets updated alongside
schema changes; once B1 lands, `scripts/refresh-db-uml.sh` (a
follow-up) regenerates this section from the live DB so it can't
drift.

## Open questions

- **Chunker token-counting**: which tokenizer? `tiktoken` (OpenAI),
  the embedder's own tokenizer, or a heuristic? Cheapest is
  characters/4. Embedder's tokenizer is most accurate but only
  available when the embedder is loaded. **Default: BGE-M3's
  XLM-R tokenizer when available, fallback to chars/4.**
- **Re-chunk policy**: when chunker rules change, do we re-chunk
  every existing ref? Cost: re-embed all chunks too. Mitigation:
  re-chunking just inserts new chunks; orphan cleanup runs as a
  periodic job. Derived-queue worker re-embeds the new chunks
  naturally.
- **`pub_id` collisions**: birthday at ~46 K refs. Detection is
  trivial (UNIQUE constraint). Resolution policy: extend to 7
  characters for the colliding ref, log a warning. Document in a
  follow-up ADR before the first collision.
- **`cite_key` collisions**: ADR 0006 specifies letter suffixes
  (`miller23a`, `miller23b`). Open: deterministic suffix (hashed)
  vs. insertion-order suffix. Start with insertion-order; upgrade
  if a real workflow surfaces.
- **Embedder dim mismatch**: `embedders.dim` declares the expected
  vector size. Worker rejects vectors of wrong dim. If a user
  changes the model behind an `embedder` name (e.g., bge-m3 v2
  with different dim), the name must change too. User-facing rule
  worth documenting in `docs/conventions/`.
- **Multi-paper PDF auto-split**: the heuristic is fragile. v2
  ships only the manual `--pages` flag. Auto-split is a follow-up.
- **Self-ingest scope (B12)**: which directories beyond
  `data/skills/` and `src/precis/` are worth ingesting? `docs/`
  yields skills + decisions + designs. `tests/` would be noisy.
  Decide alongside B12.

## Definition of done (the whole plan)

- [ ] `0001_initial.sql` applies cleanly to a fresh DB; no other
      migration files exist.
- [ ] `precis add file.pdf` produces a `refs` row with `pub_id`,
      `cite_key`, `slug`, plus `blocks` and `chunks`. No bundle
      file is written.
- [ ] `precis worker` drains derived queues: every chunk has a
      `chunk_embeddings` row (`embedder='bge-m3'`, `status='ok'`)
      and a `chunk_summaries` row (`summarizer='rake'`). Failures
      have `status='failed'` rows and are not retried.
- [ ] `precis search` returns TOON, agent confirms it parses.
- [ ] `precis watch` ingests an inbox dir end-to-end; on failure,
      PDFs go to `errors/` not `corpus/`.
- [ ] `~/work/new_papers/` re-ingested into the v2 schema; row
      counts match the corpus PDF count.
- [ ] All tests pass in the `precis-dev` container.
- [ ] `compose.yaml` runs all three services off one image
      (renamed `acatome-watch` → `precis-watch`).
- [ ] OPEN-ITEMS.md `\ufffd` mojibake item closed (re-ingestion
      passes the new ftfy roundtrip in the chunker).
- [ ] Mermaid ER diagram in this doc matches the live schema
      (manual check pre-cutover; automated by
      `scripts/refresh-db-uml.sh` post-cutover).
