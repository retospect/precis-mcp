-- ============================================================================
-- precis-mcp — discovery layer: persistent segments + per-sentence index
-- ============================================================================
-- Forward-only migration per ADR 0005. Two new tables:
--
--   ref_segments          — derived segmentation result (one row per
--                           segment per ref). Persists the DP-uniform-
--                           cost + KeyBERT pipeline output so the TOC
--                           renderer and search-result rows can serve
--                           from one SQL SELECT instead of recomputing
--                           at request time.
--
--   ref_segment_sentences — every body sentence in every segment,
--                           with its bge-m3 embedding stored in a
--                           pgvector column. Drives query-aligned
--                           excerpt selection in search results
--                           (cosine rerank against the query
--                           embedding) and enables a future corpus-
--                           wide sentence-level retrieval pass via
--                           an HNSW index (non-breaking add later).
--
-- Companion documents:
--   docs/design/storage-v2.md                 (segments + sentences
--                                              section to be added in a
--                                              follow-up doc commit)
--   docs/decisions/0005-greenfield-migrations.md
--
-- Cascade-delete rule: ref_segments → refs(ref_id), and
-- ref_segment_sentences → ref_segments(segment_id). Re-ingesting a
-- paper (which deletes its chunks) cascades through everything; the
-- worker simply re-derives on the next pass.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- ref_segments — derived per-ref segmentation
-- ---------------------------------------------------------------------------

CREATE TABLE ref_segments (
    segment_id            BIGSERIAL PRIMARY KEY,

    ref_id                BIGINT  NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    segment_idx           INT     NOT NULL,           -- 0-based within ref

    -- Block-range covered, inclusive. ``ord`` values from chunks (the
    -- v2 chunks layer is monotonic per ref). A segment that spans
    -- chunks 4..7 has pos_lo=4, pos_hi=7.
    pos_lo                INT     NOT NULL,
    pos_hi                INT     NOT NULL,

    -- ``heading`` is non-NULL when this segment corresponds to an H2
    -- heading-detected region (the TOC's three-column mode); NULL
    -- when the worker fell back to embedding-mode segmentation.
    heading               TEXT,

    -- 'h2' (heading-driven) | 'embedding' (DP-uniform-cost).
    mode                  TEXT    NOT NULL
        CHECK (mode IN ('h2', 'embedding')),

    -- Per-handler vocabulary — papers may use intro|methods|results|
    -- discussion|conclusion; skills may use overview|examples|
    -- see-also; conversations may use topic|question|resolution.
    -- Kept loose (TEXT) so each handler manages its own taxonomy.
    section_class         TEXT,

    -- Versioning columns for lazy invalidation. Mismatch on read
    -- means "row is stale, recompute and overwrite."
    segmentation_version  TEXT    NOT NULL,
    extractor_version     TEXT    NOT NULL,
    embedder_name         TEXT    NOT NULL REFERENCES embedders(name) ON UPDATE CASCADE,

    -- Mean of the segment's chunk embeddings — the centroid the
    -- keyword + sentence picks were scored against. Persisted so
    -- (a) segment-level similarity search ("find similar segments")
    -- becomes one vector-index lookup, and (b) re-rendering doesn't
    -- have to re-average chunks.
    centroid              vector(1024),

    -- Ordered keyword records. JSONB shape per element:
    --   {"long": "Metal-Organic Frameworks",
    --    "short": "MOFs",
    --    "aliases": ["MOF", "Metal-Organic Framework"],
    --    "score": 0.87}
    -- Order is matryoshka: most-distinctive first (segment centroid
    -- vs. sibling centroids, λ ≈ 0.3 penalty).
    keywords              JSONB   NOT NULL DEFAULT '[]'::jsonb,

    -- Denormalized lookup keys flattened across long/short/aliases of
    -- every entry in ``keywords``. GIN-indexed so cross-paper search
    -- by any surface form ("MOF", "MOFs", "Metal-Organic Framework")
    -- hits the index directly.
    forms                 TEXT[]  NOT NULL DEFAULT '{}',

    status                TEXT    NOT NULL DEFAULT 'ok'
        CHECK (status IN ('ok', 'failed')),
    attempts              INT     NOT NULL DEFAULT 1,
    last_error            TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (ref_id, segment_idx),
    CHECK (pos_lo <= pos_hi)
);

-- Lookup hot paths.
CREATE INDEX ref_segments_ref_id_idx    ON ref_segments (ref_id);
CREATE INDEX ref_segments_failed_idx    ON ref_segments (ref_id) WHERE status = 'failed';
CREATE INDEX ref_segments_section_idx   ON ref_segments (section_class) WHERE section_class IS NOT NULL;
CREATE INDEX ref_segments_forms_idx     ON ref_segments USING GIN (forms);

-- Segment-range lookup: when a search hit lands on a chunk we need
-- to find which segment owns it. ``pos_lo <= chunk.ord <= pos_hi``
-- combined with the GiST INT4RANGE expression below makes that an
-- index-supported query.
CREATE INDEX ref_segments_range_idx ON ref_segments USING GIST (
    ref_id,
    int4range(pos_lo, pos_hi, '[]')
);


-- ---------------------------------------------------------------------------
-- ref_segment_sentences — every body sentence with its embedding
-- ---------------------------------------------------------------------------

CREATE TABLE ref_segment_sentences (
    sentence_id                BIGSERIAL PRIMARY KEY,

    segment_id                 BIGINT  NOT NULL REFERENCES ref_segments(segment_id) ON DELETE CASCADE,
    sentence_idx               INT     NOT NULL,   -- 0-based within segment

    text                       TEXT    NOT NULL,
    chunk_pos                  INT     NOT NULL,   -- ord of the source chunk
    char_offset                INT     NOT NULL,   -- offset within the source chunk

    -- cos(sentence, segment_centroid) - λ * max(cos(sentence, sibling_centroid)).
    -- Top-N by this score serves as the TOC excerpt; the full set is
    -- query-reranked against the query embedding in search results.
    centroid_score             REAL    NOT NULL,

    -- Per-sentence embedding (bge-m3, 1024-dim). Same embedder as
    -- ref_segments.embedder_name; tracked here for self-containment
    -- and to make the future HNSW index possible.
    embedding                  vector(1024),

    -- Splitter version. Mismatch on read invalidates the row's
    -- offsets (a splitter change shifts every char_offset).
    sentence_splitter_version  TEXT    NOT NULL,

    status                     TEXT    NOT NULL DEFAULT 'ok'
        CHECK (status IN ('ok', 'failed')),
    last_error                 TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (segment_id, sentence_idx),
    CHECK (char_offset >= 0),
    CHECK (chunk_pos >= 0)
);

CREATE INDEX ref_segment_sentences_segment_idx  ON ref_segment_sentences (segment_id);
CREATE INDEX ref_segment_sentences_chunk_idx    ON ref_segment_sentences (chunk_pos);

-- Centroid-score ordering for the cheap TOC excerpt query
-- (``ORDER BY centroid_score DESC LIMIT 2`` per segment).
CREATE INDEX ref_segment_sentences_score_idx
    ON ref_segment_sentences (segment_id, centroid_score DESC);

-- HNSW index on the embedding column is deliberately NOT created
-- here. It only earns its build cost once corpus-wide sentence-
-- level retrieval becomes a real query — a non-breaking add then
-- via ``CREATE INDEX CONCURRENTLY``.
