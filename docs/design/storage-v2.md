# storage-v2 — design plan

- **Status**: draft (2026-05-21)
- **Authors**: Reto + agent
- **Related ADRs**:
  - [`0001-merge-acatome-into-precis.md`](../decisions/0001-merge-acatome-into-precis.md)
  - [`0002-pub-id-and-toon.md`](../decisions/0002-pub-id-and-toon.md)

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

| Identifier | Where | Format | Properties |
|---|---|---|---|
| `paper_id` | `refs.paper_id` | `arxiv:..` / `doi:..` / `sha256:..` | priority arxiv > doi > sha256 of pdf bytes |
| `pub_id` | `refs.pub_id` (new, primary LLM handle) | 6-char base32 lowercase | `base32(sha256(paper_id))[:6].lower()`; pinned at first ingest |
| `slug` | `refs.slug` | `<surname><year><word>` | human-readable, used as filename |
| `ref_id` | `refs.ref_id` | bigserial | internal FK target only |
| `pdf_sha256` | `pdfs.pdf_sha256` | hex SHA-256 of file bytes | dedup key for binary identity |
| `content_hash` | `pdfs.content_hash` | hex SHA-256 of normalized text | dedup key for "same paper, different bytes" |

`pub_id` is **pinned at first ingest** and never changes thereafter,
even if a more authoritative `paper_id` (e.g., DOI later discovered)
appears. Aliases live in `ref_identifiers`. Re-ingest produces the
same `pub_id` because the derivation is deterministic from
`paper_id`. (See ADR 0002 §`pub_id` derivation.)

## Schema v2

The following migrations land in order. Existing migrations
(`0001`–`0009`) are *not* edited; v2 supersedes their data through a
wipe-and-reingest, not through `ALTER`s of a populated DB. New
migrations start at `0010`.

### `0010_pub_id_and_verification.sql`

```sql
-- pub_id: primary LLM-facing handle (ADR 0002)
ALTER TABLE refs ADD COLUMN pub_id CHAR(6) UNIQUE;
COMMENT ON COLUMN refs.pub_id IS
  'base32(sha256(paper_id))[:6].lower(); pinned at first ingest';

-- human-curated metadata verification
ALTER TABLE refs ADD COLUMN human_verified_at TIMESTAMPTZ;
ALTER TABLE refs ADD COLUMN human_verified_by TEXT;
COMMENT ON COLUMN refs.human_verified_at IS
  'set when a human curator reviewed and approved the metadata';

-- retraction tracking (ADR 0002 §retractions; populate via worker)
ALTER TABLE refs ADD COLUMN retraction_status TEXT
  CHECK (retraction_status IS NULL OR
         retraction_status IN ('retracted', 'corrected', 'expression_of_concern'));
ALTER TABLE refs ADD COLUMN retracted_at      TIMESTAMPTZ;
ALTER TABLE refs ADD COLUMN retraction_reason TEXT;
ALTER TABLE refs ADD COLUMN retraction_source TEXT;
ALTER TABLE refs ADD COLUMN retraction_url    TEXT;

CREATE INDEX refs_retraction_idx ON refs (retraction_status)
  WHERE retraction_status IS NOT NULL;
```

After v2 cutover, `pub_id` becomes `NOT NULL` (we drop the nullable
constraint via a follow-up migration once the wipe is done).

### `0011_pdfs_and_pages.sql`

Multi-paper-per-PDF support. PDFs become a normalized table.

```sql
CREATE TABLE pdfs (
  pdf_sha256   CHAR(64) PRIMARY KEY,
  content_hash CHAR(64) NOT NULL,             -- SHA-256 of normalized text
  page_count   INT      NOT NULL,
  size_bytes   BIGINT   NOT NULL,
  storage_path TEXT     NOT NULL,             -- e.g. corpus/s/smith2024foo.pdf
  ingested_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX pdfs_content_hash_idx ON pdfs (content_hash);

ALTER TABLE refs ADD COLUMN pdf_sha256 CHAR(64) REFERENCES pdfs(pdf_sha256);
ALTER TABLE refs ADD COLUMN pdf_pages  INT4RANGE;
ALTER TABLE refs ADD COLUMN pdf_role   TEXT;   -- 'main', 'comment', 'reply', 'letter'

-- legacy refs.pdf_path is kept for one release as a NULL-able mirror
-- of pdfs.storage_path; dropped in 0014.
```

`pdf_pages` uses Postgres' `INT4RANGE` so `[3, 8)` means "pages 3 to
7 inclusive of start, exclusive of end". A NULL `pdf_pages` means
"the whole PDF".

### `0012_chunks_and_embeddings.sql`

The big one. Chunks become first-class; embeddings move to a
keyed-by-model side table.

```sql
-- chunks: derived from blocks, the unit of embedding
CREATE TABLE chunks (
  chunk_id     BIGSERIAL PRIMARY KEY,
  ref_id       BIGINT NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
  ord          INT    NOT NULL,                   -- ordering within ref
  block_ids    BIGINT[] NOT NULL,                 -- which blocks composed this chunk
  text         TEXT NOT NULL,
  token_count  INT,                               -- approximate, for budgeting
  section_path TEXT[] NOT NULL DEFAULT '{}',
  page_first   INT,
  page_last    INT,
  chunk_kind   TEXT NOT NULL,                     -- 'paragraph', 'figure', 'table', 'equation', 'caption', 'heading'
  meta         JSONB NOT NULL DEFAULT '{}',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (ref_id, ord)
);
CREATE INDEX chunks_ref_id_idx ON chunks (ref_id);
CREATE INDEX chunks_section_path_idx ON chunks USING GIN (section_path);

-- block_embeddings: many vectors per chunk, keyed by embedder
CREATE TABLE chunk_embeddings (
  chunk_id    BIGINT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  model_slug  TEXT   NOT NULL,                    -- e.g. 'bge-m3', 'openai-3-large'
  dim         INT    NOT NULL,
  vector      VECTOR NOT NULL,                    -- pgvector type
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (chunk_id, model_slug)
);
-- Per-model HNSW indexes are created on demand (not in this migration).

-- summaries: keyed by chunk + summarizer
CREATE TABLE chunk_summaries (
  chunk_id     BIGINT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  summarizer   TEXT   NOT NULL,                   -- 'rake' | 'gpt-4-mini' | 'qwen3-9b' | …
  text         TEXT   NOT NULL,
  prompt_hash  CHAR(64),                          -- of the prompt template used
  token_count  INT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (chunk_id, summarizer)
);

-- system table: register active embedders
CREATE TABLE embedders (
  model_slug TEXT PRIMARY KEY,                    -- 'bge-m3' (default)
  dim        INT NOT NULL,
  is_default BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO embedders (model_slug, dim, is_default)
  VALUES ('bge-m3', 1024, TRUE);
```

The legacy `blocks.embedding` column is kept for one release as a
mirror of the default embedder's vector for the chunk that contains
the block. Dropped in `0014_drop_legacy_columns.sql` after the
cutover.

### `0013_block_jobs.sql`

The work queue.

```sql
CREATE TABLE block_jobs (
  job_id        BIGSERIAL PRIMARY KEY,
  job_kind      TEXT NOT NULL,                    -- 'embed:bge-m3', 'summarize:rake', 'summarize:gpt-4-mini', …
  target_kind   TEXT NOT NULL,                    -- 'chunk' (could grow: 'ref' for retraction-checks)
  target_id     BIGINT NOT NULL,                  -- chunk_id (or ref_id for ref-level jobs)
  state         TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'running' | 'done' | 'failed'
  priority      INT NOT NULL DEFAULT 0,           -- higher = sooner
  attempts      INT NOT NULL DEFAULT 0,
  worker_id     TEXT,
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ,
  error         TEXT,
  payload       JSONB NOT NULL DEFAULT '{}',
  enqueued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (job_kind, target_kind, target_id)       -- idempotent enqueue
);

CREATE INDEX block_jobs_pending_idx
  ON block_jobs (priority DESC, job_id)
  WHERE state = 'pending';
```

Worker SQL pattern (claim-and-run):

```sql
BEGIN;
SELECT job_id, job_kind, target_id, payload
  FROM block_jobs
 WHERE state = 'pending'
 ORDER BY priority DESC, job_id
 LIMIT 1
 FOR UPDATE SKIP LOCKED;
-- if we got a row: mark running, do the work, mark done
UPDATE block_jobs SET state='running', worker_id=$1, started_at=now()
 WHERE job_id=$id;
COMMIT;
```

When work finishes, a second transaction writes the result and
marks the job done in one atomic step.

`UNIQUE (job_kind, target_kind, target_id)` makes enqueueing
idempotent — re-running ingest re-asserts pending jobs without
duplicating them.

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
multiple job kinds in parallel via threads (each job kind gets its
own thread with its own loaded model).

```python
def worker(job_kinds: list[str], poll_interval: float = 1.0):
    handlers = {
        'embed:bge-m3':       embed_handler('bge-m3'),
        'summarize:rake':     rake_handler(),
        'summarize:gpt-4-mini': llm_handler('gpt-4-mini'),
        'check_retraction:crossmark': crossmark_handler(),
    }
    while not stop_event.is_set():
        job = claim_one(job_kinds)
        if job is None:
            sleep(poll_interval); continue
        try:
            handlers[job.job_kind](job.target_id, job.payload)
            mark_done(job)
        except Exception as e:
            mark_failed(job, e)
```

Operational notes:
- A single `precis worker` process is enough for a laptop deployment.
- Multiple workers across machines: each gets a unique `worker_id`
  (hostname:pid:uuid). `FOR UPDATE SKIP LOCKED` prevents collisions.
- Failed jobs auto-retry up to N times with exponential backoff
  (handled by re-enqueue on `mark_failed` if `attempts < max`).

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

Plan to land in this order so each step is reviewable and reversible:

1. **A. Workflow scaffolding** (this PR). AGENTS.md, conventions,
   ADRs 0001 + 0002, this design doc. No code change.
2. **B. Pip-merge** (next PR). Move `acatome_extract` modules into
   `precis/ingest/`, drop the separate `acatome-extract` package
   from `pyproject.toml`. Update imports across the codebase. CLI
   surface unchanged for this step.
3. **C. Schema 0010** — `pub_id`, verification, retraction columns.
   Add `precis verify` CLI. Write `make_pub_id` helper.
4. **D. Schema 0011** — `pdfs` table + multi-page support. Update
   ingest path to populate `pdfs` and `refs.pdf_pages`. Add
   `--pages` flag to `precis add`.
5. **E. Schema 0012** — `chunks`, `chunk_embeddings`, `chunk_summaries`,
   `embedders`. Write the chunker module. Migrate ingest path to
   create chunks (still embedding synchronously for now).
6. **F. Schema 0013** — `block_jobs` queue. Convert sync embed +
   summarize calls to enqueue + return. Write `precis worker`.
7. **G. TOON output** — `precis.format.toon` module, CLI integration,
   MCP responses for tabular surfaces.
8. **H. Cutover** — wipe DB, archive `~/work/corpus/` to
   `~/work/corpus.v1.bak/`, re-ingest from
   `~/work/new_papers/` via the new `precis watch`.
9. **I. Schema 0014** — drop legacy columns (`refs.pdf_path`,
   `refs.pdf_hash`, `blocks.embedding`).
10. **J. Compose update** — ship the three-service compose file.

Each step ships its own ADR if a substantive trade-off is made. Each
step has a `precis migrate --dry-run` smoke test against a fresh DB.

## Open questions

- **Chunker token-counting**: which tokenizer? `tiktoken` (OpenAI),
  the embedder's own tokenizer, or a heuristic? Cheapest is
  characters/4. Embedder's tokenizer is most accurate but only
  available when the embedder is loaded. **Default: BGE-M3's
  XLM-R tokenizer when available, fallback to chars/4.**
- **Re-chunk policy**: when chunker rules change, do we re-chunk
  every existing ref? Cost: re-embed all chunks too. Mitigation:
  worker enqueues are idempotent; re-chunking just inserts new
  chunks and the cleanup of orphan chunks runs as a periodic job.
- **`pub_id` collisions**: birthday at ~46 K refs. Detection is
  trivial (UNIQUE constraint). Resolution policy: extend to 7
  characters for the colliding ref, log a warning. Document in a
  follow-up ADR before the first collision.
- **Embedder dim mismatch**: `embedders.dim` declares the expected
  vector size. Worker rejects vectors of wrong dim. If a user
  changes the model behind a `model_slug` (e.g., bge-m3 v2 with
  different dim), the model_slug must change too. This is a
  user-facing rule worth documenting in `docs/conventions/`.
- **Multi-paper PDF auto-split**: the heuristic is fragile. v2
  ships only the manual `--pages` flag. Auto-split is a follow-up.

## Definition of done (the whole plan)

- [ ] All ten schema migrations apply cleanly to a fresh DB.
- [ ] `precis add file.pdf` produces a ref with `pub_id`, blocks,
      chunks, and pending jobs.
- [ ] `precis worker` drains the queue: every chunk has a
      `chunk_embeddings` row (model='bge-m3') and a
      `chunk_summaries` row (summarizer='rake').
- [ ] `precis search` returns TOON, agent confirms it parses.
- [ ] `precis watch` ingests an inbox dir end-to-end without
      moving PDFs to corpus on failure.
- [ ] `~/work/new_papers/` re-ingested into the v2 schema; row
      counts match the corpus PDF count.
- [ ] All existing tests pass under the new schema (some will be
      rewritten to use `chunks` instead of `blocks`).
- [ ] `compose.yaml` runs all three services off one image.
- [ ] OPEN-ITEMS.md `\ufffd` mojibake item closed (re-ingestion
      passes the new ftfy roundtrip in the chunker).
