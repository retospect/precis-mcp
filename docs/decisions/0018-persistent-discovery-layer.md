# ADR 0018 — Persistent discovery layer (`ref_segments` + `ref_segment_sentences`)

- **Status**: **superseded by F20 (2026-06-05)** — `ref_segments` and
  `ref_segment_sentences` were dropped in migration `0011_chunk_keywords.sql`.
  Discovery now reads `chunks.keywords TEXT[]` (per-chunk KeyBERT) at request
  time. See `CLAUDE.md § "What just landed (2026-06-05)"` and
  `src/precis/workers/chunk_keywords.py`. Kept here for history; do not
  implement against this design.
- **Status (historical)**: accepted (2026-05-31)
- **Deciders**: Reto + agent
- **Extends**: [ADR 0007 — Derived queue](./0007-derived-queue-no-block-jobs.md)
  and [ADR 0017 — Derived-queue family](./0017-derived-queue-family.md).
  0007/0017 formalised the *chunk-level* derived-queue pattern
  (`chunk_embeddings`, `chunk_summaries` keyed by chunk_id). This
  ADR introduces a *ref-level* counterpart for the discovery
  layer: per-segment keywords + per-sentence embeddings that
  back `view='toc'` and the search-result excerpt sub-lines.
- **Triggered by**: the citation-filling workflow design pass
  (2026-05-31). The on-demand TOC renderer
  (`utils/toc.render_for_ref`) was recomputing DP + KeyBERT on
  every view; profiling showed ~4s per call on cold cache for
  large papers. Persisting the artifacts also unlocks the
  workflow-defining feature of the new `citation` kind:
  search-result rows that carry an inline excerpt drawn from a
  query-aligned per-sentence rerank.

## Context

The discovery-layer pipeline has three stages — segment a paper
into 3–9 topic-coherent ranges, score per-segment keywords, pick
representative sentences per segment — and three workflow
consumers:

1. **TOC view** (`view='toc'`): tabular row per segment with
   keywords + an indented excerpt sub-line.
2. **Search-result rows**: per hit, an indented excerpt picked
   from the segment containing the hit chunk, **reranked against
   the query embedding** rather than the segment centroid.
3. **Future verifier subagent**: given a candidate source chunk,
   answer "does the chunk precisely support the claim?" — needs
   the verbatim sentence and its char offset.

ADR 0017 formalised chunk-keyed derived state. The discovery
layer is *ref-keyed* — segments span multiple chunks; sentences
nest within segments. The cleanest model is a new tier of derived
state with its own ownership.

## Decision

Persist the discovery layer in two new tables, both cascade-
deleted from `refs`. Drive them with a ref-level worker that
follows the same claim-LEFT-JOIN-output pattern as ADR 0007,
just at ref granularity.

### Schema

```sql
CREATE TABLE ref_segments (
    segment_id            BIGSERIAL PRIMARY KEY,
    ref_id                BIGINT NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    segment_idx           INT NOT NULL,            -- 0-based within ref
    pos_lo                INT NOT NULL,            -- inclusive chunk.ord
    pos_hi                INT NOT NULL,
    heading               TEXT,                    -- H2 mode only
    mode                  TEXT NOT NULL
                          CHECK (mode IN ('h2','embedding')),
    section_class         TEXT,                    -- intro|methods|… (paper-specific, nullable)
    segmentation_version  TEXT NOT NULL,
    extractor_version     TEXT NOT NULL,
    embedder_name         TEXT NOT NULL REFERENCES embedders(name) ON UPDATE CASCADE,
    centroid              vector(1024),
    keywords              JSONB NOT NULL DEFAULT '[]'::jsonb,
    forms                 TEXT[] NOT NULL DEFAULT '{}',
    status                TEXT NOT NULL DEFAULT 'ok'
                          CHECK (status IN ('ok','failed')),
    attempts              INT NOT NULL DEFAULT 1,
    last_error            TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ref_id, segment_idx),
    CHECK (pos_lo <= pos_hi)
);

CREATE TABLE ref_segment_sentences (
    sentence_id                BIGSERIAL PRIMARY KEY,
    segment_id                 BIGINT NOT NULL REFERENCES ref_segments(segment_id) ON DELETE CASCADE,
    sentence_idx               INT NOT NULL,
    text                       TEXT NOT NULL,
    chunk_pos                  INT NOT NULL,        -- ord of source chunk
    char_offset                INT NOT NULL,        -- offset within source chunk
    centroid_score             REAL NOT NULL,
    embedding                  vector(1024),
    sentence_splitter_version  TEXT NOT NULL,
    status                     TEXT NOT NULL DEFAULT 'ok'
                              CHECK (status IN ('ok','failed')),
    last_error                 TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (segment_id, sentence_idx)
);
```

Migration `0005_segments_and_sentences.sql` ships both tables
plus the indexes and `CREATE EXTENSION IF NOT EXISTS btree_gist`
(required for the mixed-type GiST range index used to find "the
segment containing chunk N for this ref" in a single probe).

### Worker

`precis.workers.segment_toc.build_segments(conn, ref_id, adapter)`
runs the full pipeline per ref:

1. Boilerplate body filter (`utils.boilerplate.classify_chunks`)
2. Segmentation: H2 mode when ≥3 H2 sections cover ≥80% of body;
   DP-uniform-cost otherwise (K = ceil(body/20) clamped [3, 9])
3. Per-paper Schwartz-Hearst abbreviations
4. Paper-wide keyword scoring (no penalty)
5. Per-segment keyword scoring with **distinctiveness penalty**
   (λ ≈ 0.3 against sibling centroids) and per-segment
   exclude=(paper-wide phrases)
6. Per-sentence pysbd splitting → bge-m3 embedding → centroid
   scoring with distinctiveness penalty
7. Idempotent `DELETE FROM ref_segments WHERE ref_id=N` →
   `INSERT` of new rows (cascade on segments handles sentences)

`run_paper_segments_pass(store, embedder, *, limit)` is the
runner-side helper; `claim_refs_without_segments(conn, limit)`
implements the derived-queue claim (`refs LEFT JOIN
ref_segments`). Exposed as `precis worker --only segments`.

The worker is **not** a `WorkerHandler` subclass — that
abstraction is chunk-keyed (`output_table`, `model_column`,
`claim_batch SELECT c.chunk_id FROM chunks c LEFT JOIN
output o ON o.chunk_id = c.chunk_id`). Folding the ref-level
pattern into that base would force polymorphism over the claim
shape; cleaner to keep them as siblings.

## Alternatives considered

### A. Unified `chunk_embeddings`-style table for sentences

Treat each sentence as a chunk, store its embedding in
`chunk_embeddings` keyed by a new `chunk_kind = 'sentence'`.

**Rejected** because sentence ownership is ref→segment→sentence,
not ref→chunk→sentence. Chunk granularity is the OCR/structure
unit (a paragraph). Sentences are the meaning unit *within* a
chunk and don't belong in the same address space. Storing them
as chunks would also make the `slug~N` chunk address grammar
explode into a sentence-level `slug~N~M` form that we
explicitly rejected during design (sentence-level offsets are
fragile across splitter upgrades; quote+offset on citation
records gives sentence precision without growing the URL
grammar).

### B. JSONB embedding storage on segments

Store the full per-sentence embedding list as a JSONB array on
`ref_segments`.

**Rejected** because pgvector can't operate on vectors stored
in JSON — no `<=>` cosine, no HNSW indexability. Query-time
rerank against the query embedding requires the embedding to
be in a `vector(1024)` column. Sentences in their own table is
the natural shape.

### C. On-demand recompute with bigger cache

Keep the in-memory LRU but bump capacity from 256 → 10,000
entries.

**Rejected** because (a) cache doesn't survive restarts —
worker drains during deploys lose state, (b) cache is
per-process — every MCP stdio process boots cold, (c) the
search-result excerpt rerank wants the per-sentence
embeddings persistently indexed, not transiently held.

### D. Unify chunk + sentence embeddings in one polymorphic table

Single `embeddings` table with `(target_kind, target_id)`
discriminator + `vector` column. Use for chunk and sentence
embeddings interchangeably.

**Rejected for v1**, kept as future migration path. Reasons
recorded in [`docs/design/storage-v2.md § Open questions`]:
ownership clarity is cleaner with separate tables, the
workflow today doesn't need unified primary retrieval, and
the cross-granularity story can be handled at query time via
a `v_embeddings` UNION-ALL view + per-table HNSW (no data
migration required when we want it).

## Consequences

### Positive

- **TOC view drops from ~4s to ~50ms** for any paper whose
  segments have landed in the persistent layer.
- **Search-result rows are actionable for triage** without a
  drill-in: each row carries the segment's query-aligned best
  sentence inline.
- **Sentence-precision citation grounding** falls out for free:
  the verifier subagent gets `(text, chunk_pos, char_offset)`
  per sentence, so a `citation` record can pin the exact span.
- **Forward-compatible to sentence-level corpus retrieval**: an
  HNSW index on `ref_segment_sentences.embedding` is a
  non-breaking add when "find the sentence anywhere in the
  corpus matching X" becomes a real query.
- **Idempotent re-runs** via DELETE-then-INSERT — the worker is
  safe to drive repeatedly, no `ON CONFLICT` UPSERT logic in
  the application layer.
- **Lazy invalidation discipline**: every row carries the
  versions that produced it (`segmentation_version`,
  `extractor_version`, `embedder_name`,
  `sentence_splitter_version`). Mismatch on read means
  "treat as cache-miss, recompute, overwrite." Pipeline
  upgrades self-heal.

### Negative

- **~2 MB/paper storage cost** for the persistent sentences
  (~500 sentences × 4 KB embedding). At corpus scale (~10K
  papers) that's ~20 GB. Real but tractable; HNSW maintenance
  becomes the binding cost when corpus exceeds ~100K papers.
- **Ingest-time latency grows**: the segment_toc worker adds
  ~30s of bge-m3 work per paper after the embed worker drains.
  Mitigated by running it as a separate `--only segments`
  process so it doesn't block the chunk embedder.
- **Two worker paths** (chunk-level `WorkerHandler` and
  ref-level `run_paper_segments_pass`) instead of one. If a
  third ref-level worker shows up (e.g. a future
  retraction-recheck job under ADR 0017's family), a shared
  `RefWorkerHandler` base becomes worth extracting. For now
  the duplication is small enough to leave inline.

### Neutral

- The `forms TEXT[]` denormalization on `ref_segments` is the
  GIN-indexed lookup target for cross-paper queries by any
  keyword surface form. Adds storage but enables the
  index-supported `WHERE forms @> ARRAY['MOF']` shape we need
  for the "show me every segment that mentions any form of
  MOF" workflow. Future "concepts in the sky" canonical-
  vocabulary work (deferred per the design discussion) would
  consume this column and produce a separate concepts table.

## Implementation

Shipped 2026-05-31 in:

- `src/precis/migrations/0005_segments_and_sentences.sql`
- `src/precis/migrations/0006_chunk_numerics.sql` — the path-2
  `chunks.numerics TEXT[]` lexical numeric-token index. Lives
  alongside the segment work because it's the cheap
  quantitative-search lever the discovery layer enables
  (`WHERE numerics @> ARRAY['1.523 eV']`).
- `src/precis/migrations/0007_citation_kind.sql` — seeds
  `kind='citation'`. The verifier-workflow scaffold lands as a
  new `NumericRefHandler` subclass; see
  [`precis-citation-help`](../../src/precis/data/skills/precis-citation-help.md).
- `src/precis/workers/segment_toc.py`
- `src/precis/store/_segments_ops.py` (SegmentsMixin → Store)
- `src/precis/utils/toc_db.py` (`render_from_store`)
- `src/precis/handlers/citation.py`
- `src/precis/utils/sentences.py` (pysbd wrapper)
- `src/precis/utils/numerics.py`
- `src/precis/cli/worker.py` (`--only segments` mode)

Smoketest: `precis worker --only segments --once` against the
live corpus produced 3 segments + 544 sentences for the
`butlin26` paper, with real-keyword extraction and the
designed TOC output format (verified end-to-end before
acceptance).

## See also

- [`docs/design/storage-v2.md § Discovery layer`](../design/storage-v2.md)
- [`precis-toc-help`](../../src/precis/data/skills/precis-toc-help.md)
- [`precis-citation-help`](../../src/precis/data/skills/precis-citation-help.md)
- [`precis-search-help`](../../src/precis/data/skills/precis-search-help.md)
