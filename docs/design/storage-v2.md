# storage-v2 — design plan

- **Status**: locked (2026-05-21)
- **Authors**: Reto + agent
- **Canonical schema visual**: [`schema-v2.puml`](./schema-v2.puml) /
  [`schema-v2.svg`](./schema-v2.svg) — rendered via
  `scripts/render-uml schema-v2.puml`
- **Related ADRs**:
  - [`0001-merge-acatome-into-precis.md`](../decisions/0001-merge-acatome-into-precis.md)
  - [`0002-pub-id-and-toon.md`](../decisions/0002-pub-id-and-toon.md) (TOON portion in force; identifier section superseded by 0006, then 0008)
  - [`0005-greenfield-migrations.md`](../decisions/0005-greenfield-migrations.md)
  - [`0006-tri-identifier-scheme.md`](../decisions/0006-tri-identifier-scheme.md) (slug section superseded by 0008)
  - [`0007-derived-queue-no-block-jobs.md`](../decisions/0007-derived-queue-no-block-jobs.md)
  - [`0008-drop-slug-identifier-normalisation.md`](../decisions/0008-drop-slug-identifier-normalisation.md)
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
  cite_key-based filenames for v2; revisit when corpus exceeds 100 K
  refs).
- Backwards-compat shims for `acatome-extract` / `acatome-store`
  imports.

## Identity & naming (recap)

Per ADRs 0006 + 0008, **two user-visible identifiers** plus the
synthetic source `paper_id` plus PDF hashes:

| Identifier | Where | Format | Audience | Properties |
|---|---|---|---|---|
| `paper_id` | `ref_identifiers (id_kind='paper_id')` | `arxiv:..` / `doi:..` / `sha256:..` | internal | priority arxiv > doi > sha256 of pdf bytes |
| `pub_id` | `ref_identifiers (id_kind='pub_id')` | 6-char base32 lowercase | machines (DB, MCP, URLs) | `base32(sha256(paper_id))[:6].lower()`; pinned at first ingest |
| `cite_key` | `ref_identifiers (id_kind='cite_key')` | `miller23a` (firstauthor + 2-digit year + collision suffix) | humans (LaTeX, filenames, CLI) | minted at first ingest with collision-letter suffix |
| `ref_id` | `refs.ref_id` | bigserial | internal FK | not surfaced to clients |
| `pdf_sha256` | `pdfs.pdf_sha256` (and `ref_identifiers`) | hex SHA-256 of file bytes | internal | dedup key for binary identity |
| `content_hash` | `pdfs.content_hash` (and `ref_identifiers`) | hex SHA-256 of normalized text | internal | dedup key for "same paper, different bytes" |

**All identifiers live in `ref_identifiers`** — there are no
identifier columns on `refs`. The `v_refs` view exposes `pub_id`,
`cite_key`, `paper_id` as virtual columns for ergonomic SELECTs.
Lookup-by-any-handle is one query:

```sql
SELECT r.* FROM refs r
  JOIN ref_identifiers ri USING (ref_id)
  WHERE ri.id_value = $1;
```

`pub_id` is **pinned at first ingest** and never changes; re-ingest
of the same `paper_id` produces the same `pub_id`. `cite_key` is
immutable except for collision-suffix resolution. See ADR 0008 for
the full rationale and the deprecation of `slug`.

## Schema v2

Per ADR 0005, this is **greenfield**: a single `0001_initial.sql`
replaces the legacy `0001`–`0009` migrations. No layered ALTERs;
every column ships in its final shape from migration zero. The
sections below describe the schema in logical groupings (refs, pdfs,
chunks, embeddings, summaries, queue-via-derived-state, graph). The
actual SQL is one file — see B1 in `pip-merge.md`.

### `refs` table — identifier-free hub

Carries no identifier columns (per ADR 0008). All identifiers
live in `ref_identifiers`; ergonomic access via `v_refs` view.

```sql
CREATE TABLE refs (
  ref_id              BIGSERIAL PRIMARY KEY,
  -- classification
  kind                TEXT     NOT NULL REFERENCES kinds(slug),
  set_by              TEXT     REFERENCES actors(slug),
  -- core metadata
  title               TEXT     NOT NULL,
  authors             JSONB,                       -- list of {family, given, ...}
  year                INT,
  -- provenance
  provider            TEXT     REFERENCES providers(slug),
  -- human verification
  human_verified_at   TIMESTAMPTZ,
  human_verified_by   TEXT,
  human_verified_note TEXT,
  -- retraction tracking (this ref retracted; cited-paper retraction is derived)
  retraction_status   TEXT
    CHECK (retraction_status IS NULL OR
           retraction_status IN ('retracted', 'corrected', 'expression_of_concern')),
  retracted_at        TIMESTAMPTZ,
  retraction_reason   TEXT,
  retraction_url      TEXT,
  retraction_checked_at TIMESTAMPTZ,
  -- multi-paper-per-PDF
  pdf_sha256          CHAR(64) REFERENCES pdfs(pdf_sha256),
  pdf_pages           INT4RANGE,                   -- NULL = whole PDF
  pdf_role            TEXT,                        -- 'main' | 'supplement' | 'appendix' | 'front_matter' | 'back_matter'
  -- bookkeeping
  meta                JSONB    NOT NULL DEFAULT '{}',
  deleted_at          TIMESTAMPTZ,                 -- soft delete
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX refs_kind_idx           ON refs (kind);
CREATE INDEX refs_year_idx           ON refs (year) WHERE year IS NOT NULL;
CREATE INDEX refs_retraction_idx     ON refs (retraction_status) WHERE retraction_status IS NOT NULL;
CREATE INDEX refs_human_verified_idx ON refs (human_verified_at) WHERE human_verified_at IS NOT NULL;
CREATE INDEX refs_alive_idx          ON refs (kind, year) WHERE deleted_at IS NULL;
```

N.B. there is no `refs.title_tsv`. Full-text search on titles
happens through the `card_combined` chunk's generated `tsv`
column (which carries title + authors + abstract + keywords).
Single source of truth.

### `ref_identifiers` table — THE identifier table

All identifiers live here (per ADR 0008): primary handles
(`pub_id`, `cite_key`, `paper_id`) and external aliases (`doi`,
`arxiv`, `s2`, `pubmed`, `openalex`, `pdf_sha256`, `content_hash`).
One resolver, one index, one code path.

```sql
CREATE TABLE ref_identifiers (
  id_kind   TEXT NOT NULL,    -- 'pub_id' | 'cite_key' | 'paper_id'
                              --  | 'doi' | 'arxiv' | 's2' | 'pubmed' | 'openalex'
                              --  | 'pdf_sha256' | 'content_hash'
  id_value  TEXT NOT NULL,
  ref_id    BIGINT NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
  source    TEXT,             -- 'crossref' | 'semantic_scholar' | 'manual' | …
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (id_kind, id_value)
);
CREATE INDEX ref_identifiers_ref_id_idx ON ref_identifiers (ref_id);
```

Every ref gets two rows at insert (`pub_id`, `cite_key`); legacy
refs from acatome migration get a third (`paper_id`). External
aliases land as they're discovered (during identity resolution
from CrossRef / S2 / arXiv).

### `v_refs` view — ergonomic access

```sql
CREATE VIEW v_refs AS
SELECT r.*,
       (SELECT id_value FROM ref_identifiers
         WHERE ref_id = r.ref_id AND id_kind = 'pub_id')   AS pub_id,
       (SELECT id_value FROM ref_identifiers
         WHERE ref_id = r.ref_id AND id_kind = 'cite_key') AS cite_key,
       (SELECT id_value FROM ref_identifiers
         WHERE ref_id = r.ref_id AND id_kind = 'paper_id') AS paper_id
FROM refs r;
```

Application code generally SELECTs from `v_refs`; the three
correlated subqueries are indexed lookups.

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

### `chunks` layer (kind-agnostic; cards at `ord < 0`)

Chunks are the unit of embedding, summarisation, and search.
Kind-agnostic: any text-bearing ref (paper, skill, code symbol,
decision, memory, project) becomes chunks.

**Two-tier ord scheme:**
- `ord < 0` — synthetic ref-level cards (`chunk_kind LIKE 'card_%'`).
  Each card variant gives a different ref-level embedding
  neighbourhood. Default ingest creates **all applicable variants**.
- `ord >= 0` — body chunks (`paragraph`, `figure`, `code_symbol`, …).
  Derived from source content (Marker blocks for PDFs; per-kind for
  others).

```sql
CREATE TABLE chunks (
  chunk_id     BIGSERIAL PRIMARY KEY,
  ref_id       BIGINT NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
  set_by       TEXT   REFERENCES actors(slug),
  ord          INT    NOT NULL,                  -- <0 = card, >=0 = body
  chunk_kind   TEXT   NOT NULL REFERENCES chunk_kinds(slug),
  text         TEXT   NOT NULL,
  block_ids    BIGINT[] NOT NULL DEFAULT '{}',   -- Marker blocks; empty otherwise
  token_count  INT,
  section_path TEXT[] NOT NULL DEFAULT '{}',
  page_first   INT,                              -- NULL for cards / non-paper
  page_last    INT,                              -- NULL for cards / non-paper
  meta         JSONB  NOT NULL DEFAULT '{}',
  tsv          TSVECTOR GENERATED ALWAYS AS (
                 to_tsvector('english', text)) STORED,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (ref_id, ord),
  CHECK (
    (ord <  0 AND chunk_kind LIKE 'card_%') OR
    (ord >= 0 AND chunk_kind NOT LIKE 'card_%')
  )
);
CREATE INDEX chunks_ref_id_idx       ON chunks (ref_id);
CREATE INDEX chunks_chunk_kind_idx   ON chunks (chunk_kind);
CREATE INDEX chunks_section_path_idx ON chunks USING GIN (section_path);
CREATE INDEX chunks_tsv_idx          ON chunks USING GIN (tsv);
```

**Card variants** (all generated at default ingest when source data
permits):

| `ord` | `chunk_kind` | text source |
|---|---|---|
| `-1` | `card_combined` | title + authors + abstract + keywords + cite_key |
| `-2` | `card_title` | title only |
| `-3` | `card_authors` | normalised author list |
| `-4` | `card_abstract` | abstract only |
| `-5` | `card_meta` | DOI / journal / year / venue |
| `-6` | `card_keywords` | RAKE keywords (scispacy-lemmatised, top-50) |

`card_keywords` is derived: the worker waits for body chunks to
have RAKE summaries (`chunk_summaries WHERE summarizer='rake-lemma'`),
then aggregates the top-K keywords across the ref's body and
emits the `card_keywords` chunk. Embedding follows automatically
via the derived queue.

Full-text search uses `chunks.tsv` (GIN-indexed). Hybrid retrieval
combines `ts_rank_cd(tsv, q)` with vector similarity via RRF (see
"Search strategy" below).

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

### `embedders` and `summarizers` registries

```sql
CREATE TABLE embedders (
  name          TEXT PRIMARY KEY,
  dim           INT  NOT NULL,
  is_default    BOOLEAN NOT NULL DEFAULT FALSE,
  description   TEXT,
  deprecated_at TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_default_embedder ON embedders (is_default) WHERE is_default = TRUE;

INSERT INTO embedders (name, dim, is_default, description) VALUES
  ('bge-m3', 1024, TRUE, 'BAAI/bge-m3, dense; 1024-dim; multilingual');

CREATE TABLE summarizers (
  name            TEXT PRIMARY KEY,
  prompt_template TEXT,
  config          JSONB NOT NULL DEFAULT '{}',
  is_default      BOOLEAN NOT NULL DEFAULT FALSE,
  description     TEXT,
  deprecated_at   TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_default_summarizer ON summarizers (is_default) WHERE is_default = TRUE;

INSERT INTO summarizers (name, config, is_default, description) VALUES
  ('rake-lemma',
   '{"lemmatizer": "scispacy", "model": "en_core_sci_sm", "max_keywords": 50,
     "min_phrase_words": 1, "max_phrase_words": 4}'::jsonb,
   TRUE,
   'RAKE phrase extraction + scispacy lemmatisation');
```

**Active embedder = `embedders.is_default=TRUE`.** Exactly one row
is active (partial UNIQUE index enforces). Default search uses
this row; users never specify an embedder unless opting out.
Changing the active embedder is one `UPDATE … is_default` in a
transaction; worker re-embeds chunks lazily.

**Multi-embedder support:** the schema allows multiple registered
embedders. Today all must share the dim of the default (1024) so
`chunk_embeddings.vector` is `vector(1024)`. When a different-dim
embedder is needed (e.g., SPECTER2 at 768 for paper-specific
search), partition `chunk_embeddings` by `embedder` (LIST
partitioning) and give each partition its own dim. Migration is
straightforward; defer until needed.

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
              │   - compute paper_id, pub_id, cite_key      │
              │   - dedup via ref_identifiers               │
              └────────────────────────────────────────────┘
                              │
                              ▼
              ┌────────────────────────────────────────────┐
              │  store_pdf()  (if any)                      │
              │   - copy to corpus/<letter>/<cite_key>.pdf  │
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

## Discovery layer (segments + sentences + numerics + citations)

Added 2026-05-31. Migrations `0005_segments_and_sentences.sql`,
`0006_chunk_numerics.sql`, `0007_citation_kind.sql`. See the
design discussion threaded through this conversation for the
full reasoning.

### `ref_segments` table

Persistent per-segment artifacts. The TOC renderer reads from
here instead of recomputing DP + KeyBERT at request time.

```sql
CREATE TABLE ref_segments (
    segment_id            BIGSERIAL PRIMARY KEY,
    ref_id                BIGINT  NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    segment_idx           INT     NOT NULL,
    pos_lo                INT     NOT NULL,         -- inclusive chunk.ord
    pos_hi                INT     NOT NULL,
    heading               TEXT,                     -- H2 mode only
    mode                  TEXT    NOT NULL CHECK (mode IN ('h2','embedding')),
    section_class         TEXT,                     -- intro|methods|results|… (paper-specific; nullable)
    segmentation_version  TEXT    NOT NULL,
    extractor_version     TEXT    NOT NULL,
    embedder_name         TEXT    NOT NULL REFERENCES embedders(name) ON UPDATE CASCADE,
    centroid              vector(1024),             -- segment centroid for similarity search
    keywords              JSONB   NOT NULL DEFAULT '[]'::jsonb,
    forms                 TEXT[]  NOT NULL DEFAULT '{}',
    status                TEXT    NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','failed')),
    attempts              INT     NOT NULL DEFAULT 1,
    last_error            TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ref_id, segment_idx),
    CHECK (pos_lo <= pos_hi)
);
CREATE INDEX ref_segments_ref_id_idx  ON ref_segments (ref_id);
CREATE INDEX ref_segments_failed_idx  ON ref_segments (ref_id) WHERE status = 'failed';
CREATE INDEX ref_segments_section_idx ON ref_segments (section_class) WHERE section_class IS NOT NULL;
CREATE INDEX ref_segments_forms_idx   ON ref_segments USING GIN (forms);
CREATE INDEX ref_segments_range_idx   ON ref_segments USING GIST (ref_id, int4range(pos_lo, pos_hi, '[]'));
```

`keywords` is a JSONB list of `{long, short, aliases[], score}`
records ordered by **distinctiveness** (most-distinctive first —
`cos(phrase, segment_centroid) - λ·max(cos(phrase, sibling))`).
`forms` is the denormalized GIN-indexed lookup target across every
surface form. The GiST range index (requires `btree_gist`) supports
the "segment containing chunk N" lookup used by the search-result
cluster-context hint.

### `ref_segment_sentences` table

Every body sentence with its bge-m3 embedding. Stored exhaustively
(not top-K) because the embedding compute happens regardless during
sentence scoring; the marginal storage cost (~2 MB / paper) buys
both TOC-time excerpts (`ORDER BY centroid_score DESC LIMIT 2`) and
search-time query-aligned excerpts (`ORDER BY embedding <=> query`).

```sql
CREATE TABLE ref_segment_sentences (
    sentence_id                BIGSERIAL PRIMARY KEY,
    segment_id                 BIGINT  NOT NULL REFERENCES ref_segments(segment_id) ON DELETE CASCADE,
    sentence_idx               INT     NOT NULL,
    text                       TEXT    NOT NULL,
    chunk_pos                  INT     NOT NULL,
    char_offset                INT     NOT NULL,
    centroid_score             REAL    NOT NULL,
    embedding                  vector(1024),
    sentence_splitter_version  TEXT    NOT NULL,
    status                     TEXT    NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','failed')),
    last_error                 TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (segment_id, sentence_idx),
    CHECK (char_offset >= 0),
    CHECK (chunk_pos >= 0)
);
CREATE INDEX ref_segment_sentences_segment_idx ON ref_segment_sentences (segment_id);
CREATE INDEX ref_segment_sentences_chunk_idx   ON ref_segment_sentences (chunk_pos);
CREATE INDEX ref_segment_sentences_score_idx   ON ref_segment_sentences (segment_id, centroid_score DESC);
-- HNSW on embedding is a future non-breaking add when sentence-
-- level corpus retrieval becomes a real query.
```

### Worker

`precis.workers.segment_toc.build_segments(conn, ref_id, adapter)`
runs the full pipeline per ref: boilerplate body filter →
segmentation (H2 mode when ≥3 sections cover ≥80%; DP-uniform-cost
otherwise) → per-paper Schwartz-Hearst abbreviations → paper-wide
keywords → per-segment keywords with distinctiveness penalty →
all-body sentences with per-sentence bge-m3 embeddings + centroid
scores → idempotent DELETE+INSERT.

Driven by `precis worker --only segments` which calls
`claim_refs_without_segments` (refs LEFT JOIN ref_segments) and
runs each through `run_paper_segments_pass`. Not a `WorkerHandler`
subclass because it's ref-keyed, not chunk-keyed.

### `chunks.numerics` lexical token index

```sql
ALTER TABLE chunks ADD COLUMN numerics TEXT[] NOT NULL DEFAULT '{}';
CREATE INDEX chunks_numerics_idx ON chunks USING GIN (numerics);
```

`precis.utils.numerics.extract_numerics(text)` pulls every
`<number><unit>` token from a closed unit vocab at ingest time
(eV/V/A/Hz/cm⁻¹/%/K/°C/Pa/M/nm/cycles/s/…, longest-first match).
Stored on the chunks row. GIN-indexed for exact lookups
(`WHERE numerics @> ARRAY['1.523 eV']`). Path-2 from the tables
discussion; structured `paper_facts` extraction (path-3) remains
deferred.

### References detection at ingest

`pipeline._retag_references(chunks)` runs the boilerplate
classifier over body chunks and rewrites detected references rows
to `chunk_kind='references'` before insert. The embedder + RAKE
workers carry `skip_chunk_kinds = ('references',)` which extends
the claim SQL with `AND c.chunk_kind <> ALL(%s)`, so references
never enter the work queue. Bibliography stops polluting search
without needing per-handler filter logic on the read side.

### `citation` kind

Migration `0007_citation_kind.sql` seeds `kind='citation'`
(`is_numeric=TRUE`). `CitationHandler` (extends
`NumericRefHandler`) supports write-once `put(text=<claim>,
source_handle, source_quote, char_offset, verifier_confidence,
verifier_caveats, verified_at, link='paper:<slug>', rel='cites')`
plus `get` / `search` / `delete`. Record lives in `refs.meta`;
optional `link='paper:<slug>'` writes the `cites` graph edge.

The verifier itself is client-side workflow (a subagent the
writing thread spawns); this kind only owns the storage door.
See `precis-citation-help` for the agent surface.

### Sentence splitter + dehyphenation + chunker version

`precis.utils.sentences.split_sentences` wraps pysbd 0.3.4 with
char-offset bookkeeping. `precis.ingest.text_chunker` uses it via
a `SENTENCE_SEPARATOR` sentinel in the recursive splitter's
fallback chain — abbreviations like "et al.", "Fig.", "i.e."
no longer cause mid-clause splits.

`precis.ingest.marker._clean_text` gains a regex pass joining
`-\s*\n\s*` when both sides are lowercase ASCII, preserving
semantically-significant hyphens (Z-scheme, Cu-MOF) and never
crossing paragraph breaks.

`precis.ingest.text_chunker.CHUNKER_VERSION` is the canonical
chunker version string (currently `2.0+pysbd-0.3-1`), surfaced
through `PaperHandler.chunks_for_toc` so the persistent layer
can lazy-invalidate on chunker upgrades.

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
precis show <handle>     # ref detail (handle = pub_id | cite_key | DOI | arxiv | ...)
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
  `make_pub_id`, `make_cite_key`, `make_node_id`, `make_pdf_hash`,
  `make_content_hash`. Pure functions, fully unit-tested. (No
  `make_slug`; dropped per ADR 0008.)
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
  `acatome-watch` to `precis-watch`. ✅ done
- **B10** TOON output (`precis.format.toon`). ✅ done
- **B11** Cutover — apply `0001_initial.sql` to a fresh DB,
  archive old corpus, re-ingest via `precis watch`.
- **B12** *(deferred)* `precis ingest-self` — walk
  `src/precis/data/skills/`, `src/precis/`, `docs/decisions/`,
  `docs/design/` and ingest as `kind in (skill, code_symbol,
  decision, design)`. Re-review the `chunks.chunk_kind` enum
  before this lands.
- **B13** *(shipped 2026-05-31)* **Discovery layer** — migrations
  0005/0006/0007, `precis.workers.segment_toc`,
  `precis.store._segments_ops`, `precis.utils.toc_db`,
  `precis.handlers.citation`, pysbd integration, dehyphenation,
  references-detection at ingest, numeric-token lexical index.
  Detail in the **Discovery layer** section above.

Each step ships its own commit with tests. Schema-touching steps
run `precis migrate --dry-run` against a fresh DB.

## Schema diagram

The canonical schema visual is
[`schema-v2.puml`](./schema-v2.puml) (PlantUML source) rendered to
[`schema-v2.svg`](./schema-v2.svg) and `.png`. Regenerate via
`scripts/render-uml schema-v2.puml` after edits.

The PUML carries all 16 entities (vocab + registries + hub +
chunks + graph + tags), all relationships, and inline notes
explaining card pattern, two-hash PDF dedup, identifier
normalisation, search strategy, and design decisions. It is the
spec; the SQL in `migrations/0001_initial.sql` is its
realisation.

Once B1 lands, `scripts/refresh-db-uml` (a follow-up) will
introspect the live DB and regenerate a second PUML
(`schema-actual.puml`) so spec-vs-reality drift is caught
automatically.

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
  trivial (UNIQUE constraint on `ref_identifiers (id_kind, id_value)`).
  Resolution policy: extend to 7 characters for the colliding ref,
  log a warning. Document in a follow-up ADR before the first
  collision.
- **`cite_key` collisions**: ADR 0006 specifies letter suffixes
  (`miller23a`, `miller23b`). Open: deterministic suffix (hashed)
  vs. insertion-order suffix. Start with insertion-order; upgrade
  if a real workflow surfaces.
- **`chunk_kind` hierarchy**: should the vocab table grow a
  `parent_kind` column so we can ask "all card variants" or "all
  per-kind body chunks" without LIKE matching? Currently the
  `is_card` boolean covers the most common partition; defer
  hierarchy until a real query needs it.
- **Multi-embedder different-dim**: when SPECTER2 (768) or
  NV-Embed (4096) gets registered, partition `chunk_embeddings`
  by `embedder` (LIST partitioning) and give each partition its
  own `vector(N)` column. Defer.
- **Tag rollup views performance**: `v_ref_tags_all` and
  `v_chunk_tags_all` are plain views; if hot paths emerge,
  promote to MATERIALIZED with REFRESH on a cron. Defer.
- **Multi-paper PDF auto-split**: the heuristic is fragile. v2
  ships only the manual `--pages` flag. Auto-split is a follow-up.
- **Self-ingest scope (B12)**: which directories beyond
  `data/skills/` and `src/precis/` are worth ingesting? `docs/`
  yields skills + decisions + designs. `tests/` would be noisy.
  Decide alongside B12.
- **Sentence embeddings — unified vector table?** Today
  `chunk_embeddings.vector` (chunks) and
  `ref_segment_sentences.embedding` (sentences) live in separate
  tables. Same dim, same embedder; FK cascade ownership differs.
  Unifying them would let one HNSW serve corpus-wide search at
  any granularity. Decided **defer**: the workflow searches at
  chunk granularity and reranks sentences within hits — the
  unified index isn't needed yet. When it is, the path is a
  `v_embeddings` view UNION-ALL with a `source_kind`
  discriminator + per-table HNSW indexes (no migration needed).
- **Structured table facts (task #63)**: Haiku-extracted
  `(entity, property, value, unit, qualifiers)` rows would
  enable SQL range queries like
  `WHERE property='bandgap' AND unit='eV' AND value BETWEEN
  1.3 AND 1.6`. Hard part is schema design (unit normalization,
  uncertainty, qualifiers, property aliases). Defer until a
  real quantitative-survey workflow demands it; the cheap
  precursor (`chunks.numerics`) ships with the discovery layer.
- **Cross-kind corpus search (task #66)**: today's search
  requires a `kind=` filter, so noisy kinds can't pollute paper
  search. Kind-less corpus-wide search needs per-kind weighting
  in the rank fusion (paper > decision > skill > conversation
  > perplexity). Defer until the "literature review across all
  notes" workflow surfaces.

## Definition of done (the whole plan)

- [ ] `0001_initial.sql` applies cleanly to a fresh DB; no other
      migration files exist.
- [ ] `precis add file.pdf` produces a `refs` row, two
      `ref_identifiers` rows (`pub_id`, `cite_key`), and
      `chunks` (card variants + body). No bundle file is written.
- [ ] `precis worker` drains derived queues: every chunk has a
      `chunk_embeddings` row (`embedder='bge-m3'`, `status='ok'`)
      and a `chunk_summaries` row (`summarizer='rake-lemma'`).
      Failures have `status='failed'` rows and are not retried.
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
- [ ] `schema-v2.puml` matches the live schema (manual check
      pre-cutover; automated by `scripts/refresh-db-uml`
      post-cutover).
