-- ===========================================================================
-- 0004_finding_and_queue_family.sql — finding kind + derived-queue family.
--
-- Implements C1 of docs/design/finding-chase.md and the substrate from
-- ADR 0017. Additive only — no ALTER on existing tables.
--
-- Two concerns in one migration because the finding chase is the first
-- consumer of the queue family; landing them together avoids a
-- never-used registry row.
--
-- Parts:
--   1. artifact_kinds — handler registry (ADR 0017 §3)
--   2. ref_artifacts  — per-ref untyped derived state (ADR 0017 §1)
--   3. kinds          — new ref kind 'finding'
--   4. chunk_kinds    — finding_body + finding_context
--   5. relations      — misattributes + misattributed-by (mis-citation
--                       flagging written by the chase worker; reusable
--                       outside findings)
--   6. actors         — chase worker actor (audit trail)
--
-- Retraction tracking is NOT in this migration — the provenance kind
-- (0002_provenance.sql, 0003_provenance_rw_cache.sql, plus the
-- handlers/ingest modules) owns that work synchronously, not via the
-- queue family. See ADR 0017 §"Decision > 3" for the cross-reference.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 1. artifact_kinds — handler registry
-- ---------------------------------------------------------------------------
-- Indexes the worker's view: every artifact a handler produces has a row
-- here, naming its target kind and where its output lands. Used by
-- `precis worker --status` (one row per artifact) and by the WorkerHandler
-- base class to read its claim shape (per ADR 0017 §4).
--
-- ``storage`` discriminates typed-output tables (chunk_embeddings,
-- chunk_summaries — dedicated columns for HNSW/FTS indexing) from
-- untyped JSONB-payload tables (<target>_artifacts). See ADR 0017
-- §"Decision > 2" for why typed outputs aren't folded into the family.

CREATE TABLE artifact_kinds (
    slug          TEXT PRIMARY KEY,
    target        TEXT NOT NULL
        CHECK (target IN ('chunk', 'ref', 'link', 'pdf')),
    storage       TEXT NOT NULL
        CHECK (storage IN ('typed', 'untyped')),
    output_table  TEXT NOT NULL,
    description   TEXT,
    deprecated_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed: the two existing chunk-typed artifacts that already write to
-- their dedicated output tables. Registering them here means
-- `precis worker --status` walks one place to enumerate the world.
-- Existing FKs on chunk_embeddings.embedder (→ embedders.name) and
-- chunk_summaries.summarizer (→ summarizers.name) are unchanged; the
-- new FK from <target>_artifacts.artifact to artifact_kinds.slug is
-- a separate concern (queue + observability, not model loading).
INSERT INTO artifact_kinds (slug, target, storage, output_table, description) VALUES
    ('embed:bge-m3',         'chunk', 'typed',
        'chunk_embeddings',  'BGE-M3 1024-dim dense vector'),
    ('summarize:rake-lemma', 'chunk', 'typed',
        'chunk_summaries',   'RAKE keyword summary (scispacy-lemmatised)'),
    -- Finding-chase artifacts — first untyped users of the family.
    ('chase_citation',       'ref',   'untyped',
        'ref_artifacts',     'Citation-chase pass result (one hop or terminal)'),
    ('resolve_citation:s2',  'ref',   'untyped',
        'ref_artifacts',     'Semantic Scholar metadata enrichment for stub refs');


-- ---------------------------------------------------------------------------
-- 2. ref_artifacts — per-ref untyped derived state
-- ---------------------------------------------------------------------------
-- Shape per ADR 0017 §1. Same columns the future link_artifacts /
-- pdf_artifacts / chunk_artifacts tables will use, varying only in PK
-- type and FK reference.
--
-- Claim shape (per ADR 0017 §4):
--   FROM refs r LEFT JOIN ref_artifacts o
--     ON o.ref_id = r.ref_id AND o.artifact = $1
--    WHERE o.ref_id IS NULL
--    ORDER BY r.ref_id
--    FOR UPDATE OF r SKIP LOCKED
--
-- Failure-marker rows (status='failed') prevent poison-pill re-claims;
-- the predicate ``WHERE o.ref_id IS NULL`` skips them naturally. Manual
-- retry is DELETE FROM ref_artifacts WHERE ref_id=$1 AND artifact=$2
-- AND status='failed'.

CREATE TABLE ref_artifacts (
    ref_id      BIGINT      NOT NULL
        REFERENCES refs(ref_id) ON DELETE CASCADE,
    artifact    TEXT        NOT NULL
        REFERENCES artifact_kinds(slug) ON UPDATE CASCADE,
    payload     JSONB,
    status      TEXT        NOT NULL DEFAULT 'ok'
        CHECK (status IN ('ok', 'failed')),
    attempts    INT         NOT NULL DEFAULT 1,
    last_error  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, artifact)
);
CREATE INDEX ref_artifacts_failed_idx
    ON ref_artifacts (ref_id, artifact)
    WHERE status = 'failed';
-- Lookup-by-artifact helper for `precis worker --status` aggregations and
-- for handlers that want "everything tagged X across the corpus".
CREATE INDEX ref_artifacts_artifact_idx
    ON ref_artifacts (artifact);


-- ---------------------------------------------------------------------------
-- 3. kinds — new ref kind 'finding'
-- ---------------------------------------------------------------------------
-- Numeric-id ref (like memory/todo/gripe — addressed by numeric id at
-- the protocol layer; the underlying pub_id/cite_key still apply).
-- Findings are the synthesised endpoint of a citation chase carrying
-- a claim text + setup context + provenance chain back to a primary
-- source. See docs/design/finding-chase.md §"What a finding is".

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('finding', TRUE, 'Finding',
     'A retrievable empirical claim with explicit setup context and '
     'a provenance chain back to its primary source. Synthesised by '
     'the citation-chase worker; never externally citable (see '
     'docs/design/finding-chase.md).');


-- ---------------------------------------------------------------------------
-- 4. chunk_kinds — finding_body + finding_context
-- ---------------------------------------------------------------------------
-- Two body chunks per finding ref: the claim itself (ord=0) and the
-- setup envelope (ord=1). Both embeddable and full-text-searchable
-- through the standard chunk pipeline. The split matters because
-- "what setups used 2.4 kV?" is a real query distinct from "what
-- values did Cu electrodes give?" — separate chunks → separate
-- embeddings → independent retrieval.

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('finding_body',    FALSE,
     'Finding claim text (the measured value plus its bare conditions)'),
    ('finding_context', FALSE,
     'Finding setup envelope (instrument, electrode, ambient, technique, geometry)');


-- ---------------------------------------------------------------------------
-- 5. relations — misattributes + misattributed-by
-- ---------------------------------------------------------------------------
-- Written by the chase worker when an LLM comparison of citing vs
-- cited content detects a substantive mismatch ("paper A says paper B
-- reported Cu foil; paper B actually reported Cu top contact"). The
-- relation is generic — any chunk-to-chunk misrepresentation
-- detection can land here, not just chase-driven ones. See
-- docs/design/finding-chase.md §"Mis-citation flagging".

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('misattributes',    FALSE, 'misattributed-by',
        'Source chunk misrepresents what the target chunk actually says'),
    ('misattributed-by', FALSE, 'misattributes',
        'Source chunk is misrepresented by the linked source chunk');


-- ---------------------------------------------------------------------------
-- 6. actors — chase worker actor (audit trail)
-- ---------------------------------------------------------------------------
-- Every link / ref-update the chase worker writes pins set_by='chase'.
-- Audit query: SELECT * FROM links WHERE set_by='chase'. Distinct from
-- the existing 'system' actor so chase-specific behaviour is filterable
-- without false positives from boot-time sweeps and default-tag layers.

INSERT INTO actors (slug, description) VALUES
    ('chase',
     'Citation-chase worker — automated agent that traces findings '
     'to their primary sources and flags misattributions along the '
     'chain. See docs/design/finding-chase.md.');


-- ===========================================================================
-- End of 0004_finding_and_queue_family.sql
-- ===========================================================================
