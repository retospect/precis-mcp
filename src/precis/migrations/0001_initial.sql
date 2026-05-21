-- ============================================================================
-- precis-mcp v2 — initial schema (0001_initial.sql)
-- ============================================================================
-- Greenfield migration per ADR 0005: a single sealed file ships the whole
-- v2 schema. Forward-only — to evolve, add 0002_*.sql, never edit this file.
--
-- Companion documents (read these before editing this file's successor):
--   docs/design/storage-v2.md                 prose + worker pipeline
--   docs/design/schema-v2.puml + .svg         canonical visual
--   docs/decisions/0005-greenfield-migrations.md
--   docs/decisions/0006-tri-identifier-scheme.md  (slug section superseded by 0008)
--   docs/decisions/0007-derived-queue-no-block-jobs.md
--   docs/decisions/0008-drop-slug-identifier-normalisation.md
--   docs/decisions/0010-postgres-pgvector-system-of-record.md
--
-- The migration runner (src/precis/store/migrate.py) wraps this file in a
-- transaction and inserts a row into `_migrations` on success.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- 1. Postgres version assertion
--    pgvector, generated tsvector columns, INT4RANGE, NULLS NOT DISTINCT, and
--    other features used below all require PG >= 16.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF current_setting('server_version_num')::int < 160000 THEN
    RAISE EXCEPTION
      'precis-mcp requires Postgres >= 16; this server is %',
      current_setting('server_version');
  END IF;
END$$;


-- ---------------------------------------------------------------------------
-- 2. Extensions
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;


-- ---------------------------------------------------------------------------
-- 3. Migration ledger
-- ---------------------------------------------------------------------------
CREATE TABLE _migrations (
    version    TEXT         PRIMARY KEY,
    applied_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    checksum   TEXT         NOT NULL
);


-- ===========================================================================
-- 4. Controlled vocabulary (closed sets; FK targets; seeded inline)
-- ===========================================================================

CREATE TABLE actors (
    slug        TEXT PRIMARY KEY,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO actors (slug, description) VALUES
    ('agent',  'LLM-mediated tool call'),
    ('user',   'Direct human invocation (CLI, ops)'),
    ('system', 'Server-side automation: sweeps, derived state, defaults');


CREATE TABLE kinds (
    slug          TEXT PRIMARY KEY,
    is_numeric    BOOLEAN NOT NULL DEFAULT FALSE,
    title         TEXT    NOT NULL,
    description   TEXT,
    deprecated_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    -- canonical, cite_key-addressed knowledge
    ('paper',           FALSE, 'Paper',             'Research paper, addressed by cite_key'),
    ('book',            FALSE, 'Book',              'Book or monograph'),
    ('patent',          FALSE, 'Patent',            'Patent document'),
    ('research_report', FALSE, 'Research report',   'Research / industry report'),
    -- agent / human authorship
    ('oracle', FALSE, 'Oracle',       'Oracle / authority node'),
    ('skill',  FALSE, 'Skill',        'Agent skill document'),
    ('tool',   FALSE, 'Tool',         'Tool spec or interface description'),
    ('code',   FALSE, 'Code symbol',  'Function, class, module, or repo symbol'),
    -- decision / design / project bookkeeping
    ('decision', FALSE, 'Decision', 'ADR-style decision log entry'),
    ('design',   FALSE, 'Design',   'Design document / plan'),
    ('project',  FALSE, 'Project',  'Project descriptor (goals, status, …)'),
    -- collaboration artifacts
    ('conv',    FALSE, 'Conversation', 'Conversation transcript'),
    ('meeting', FALSE, 'Meeting',      'Meeting notes / transcript'),
    ('email',   FALSE, 'Email',        'Email message or thread'),
    ('repo',    FALSE, 'Repo',         'Source-code repository'),
    ('issue',   FALSE, 'Issue',        'Issue tracker item'),
    -- ephemeral, numeric-id
    ('quest',  FALSE, 'Quest',     'Request queue item'),
    ('todo',   TRUE,  'Todo',      'Task / action item'),
    ('memory', TRUE,  'Memory',    'Note, decision, idea, claim'),
    ('gripe',  TRUE,  'Gripe',     'Informal log entry'),
    ('fc',     TRUE,  'Flashcard', 'Spaced-repetition flashcard'),
    -- paid-tool caches (cite_key-addressed, hash-derived)
    ('web',     FALSE, 'Web query',   'Cached web / research / think query'),
    ('youtube', FALSE, 'YouTube',     'Cached YouTube transcript'),
    ('math',    FALSE, 'Math result', 'Cached Wolfram math result');


CREATE TABLE relations (
    slug          TEXT PRIMARY KEY,
    is_symmetric  BOOLEAN NOT NULL DEFAULT FALSE,
    inverse_slug  TEXT,
    description   TEXT,
    deprecated_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('related-to',      TRUE,  NULL,              'Symmetric association'),
    ('blocks',          FALSE, 'blocked-by',      'Source blocks target'),
    ('blocked-by',      FALSE, 'blocks',          'Source is blocked by target'),
    ('contradicts',     FALSE, 'contradicted-by', 'Source contradicts target'),
    ('contradicted-by', FALSE, 'contradicts',     'Source is contradicted by target'),
    ('cites',           FALSE, 'cited-by',        'Source cites target'),
    ('cited-by',        FALSE, 'cites',           'Source is cited by target'),
    ('supersedes',      FALSE, 'superseded-by',   'Source supersedes target'),
    ('superseded-by',   FALSE, 'supersedes',      'Source is superseded by target');


CREATE TABLE providers (
    slug          TEXT PRIMARY KEY,
    description   TEXT,
    deprecated_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO providers (slug, description) VALUES
    -- paper sources
    ('arxiv',     'arXiv preprint server'),
    ('crossref',  'Crossref DOI metadata'),
    ('s2',        'Semantic Scholar'),
    ('pubmed',    'PubMed / NCBI'),
    ('openalex',  'OpenAlex'),
    ('unpaywall', 'Unpaywall OA index'),
    -- paid web tools
    ('perplexity', 'Perplexity (web / research / think)'),
    ('wolfram',    'Wolfram Alpha math'),
    ('youtube',    'YouTube transcript'),
    -- other
    ('manual', 'Manually uploaded'),
    ('local',  'Local computation / no external source');


CREATE TABLE chunk_kinds (
    slug          TEXT PRIMARY KEY,
    is_card       BOOLEAN NOT NULL DEFAULT FALSE,
    description   TEXT,
    deprecated_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    -- cards (ref-level synthetic chunks; ord < 0)
    ('card_combined', TRUE, 'Title + authors + abstract + keywords + cite_key'),
    ('card_title',    TRUE, 'Title only'),
    ('card_authors',  TRUE, 'Normalised author list'),
    ('card_abstract', TRUE, 'Abstract only'),
    ('card_meta',     TRUE, 'DOI / journal / year / venue'),
    ('card_keywords', TRUE, 'RAKE keywords (scispacy-lemmatised, top-50)'),
    -- generic body chunks (ord >= 0)
    ('paragraph',   FALSE, 'Body paragraph'),
    ('figure',      FALSE, 'Figure caption + reference'),
    ('equation',    FALSE, 'Inline or display equation'),
    ('caption',     FALSE, 'Table / figure caption'),
    ('heading',     FALSE, 'Section heading (rarely standalone)'),
    ('references',  FALSE, 'Bibliography section (excluded from default embedding)'),
    ('code_symbol', FALSE, 'Function / class / module body'),
    -- per-kind body chunks (text-bearing subordinate units)
    ('memory_body',           FALSE, 'Memory body text'),
    ('gripe_body',            FALSE, 'Gripe body text'),
    ('todo_body',             FALSE, 'Todo body text'),
    ('quest_body',            FALSE, 'Quest body text'),
    ('fc_claim',              FALSE, 'Flashcard claim side'),
    ('fc_evidence',           FALSE, 'Flashcard evidence side'),
    ('conv_message',          FALSE, 'Single message in a conversation'),
    ('qa_pair',               FALSE, 'Question + answer pair'),
    ('skill_overview',        FALSE, 'Skill overview section'),
    ('skill_input',           FALSE, 'Skill input description'),
    ('skill_output',          FALSE, 'Skill output description'),
    ('skill_example',         FALSE, 'Skill example'),
    ('tool_overview',         FALSE, 'Tool overview section'),
    ('tool_input_schema',     FALSE, 'Tool input schema'),
    ('tool_output_schema',    FALSE, 'Tool output schema'),
    ('tool_example',          FALSE, 'Tool example'),
    ('web_paragraph',         FALSE, 'Paragraph from a cached web result'),
    ('web_section',           FALSE, 'Section from a cached web result'),
    ('web_citation',          FALSE, 'Citation from a cached web result'),
    ('youtube_segment',       FALSE, 'YouTube transcript segment'),
    ('wolfram_query',         FALSE, 'Wolfram query text'),
    ('wolfram_response',      FALSE, 'Wolfram response text'),
    ('decision_section',      FALSE, 'Section of a decision log entry'),
    ('design_section',        FALSE, 'Section of a design document'),
    ('patent_claim',          FALSE, 'Individual patent claim'),
    ('patent_section',        FALSE, 'Patent section (description / drawings)'),
    ('project_goal',          FALSE, 'Project goal entry'),
    ('project_constraint',    FALSE, 'Project constraint entry'),
    ('project_decision_log',  FALSE, 'Project decision-log entry'),
    ('project_status',        FALSE, 'Project status entry'),
    ('project_open_question', FALSE, 'Project open question'),
    ('project_milestone',     FALSE, 'Project milestone'),
    ('meeting_segment',       FALSE, 'Meeting transcript segment'),
    ('action_item',           FALSE, 'Action item from a meeting'),
    ('meeting_decision',      FALSE, 'Decision recorded in a meeting'),
    ('email_message',         FALSE, 'Email message body'),
    ('email_attachment_ref',  FALSE, 'Reference to an email attachment'),
    ('readme_section',        FALSE, 'README section'),
    ('commit_message',        FALSE, 'Commit message'),
    ('issue_comment',         FALSE, 'Comment on an issue'),
    ('issue_label_change',    FALSE, 'Label change on an issue'),
    ('issue_milestone',       FALSE, 'Milestone change on an issue'),
    ('research_report_summary',  FALSE, 'Research-report summary section'),
    ('research_report_citation', FALSE, 'Research-report citation entry');


-- ===========================================================================
-- 5. Model registries
-- ===========================================================================

CREATE TABLE embedders (
    name          TEXT PRIMARY KEY,
    dim           INT  NOT NULL,
    is_default    BOOLEAN NOT NULL DEFAULT FALSE,
    description   TEXT,
    deprecated_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX embedders_one_default_idx
    ON embedders (is_default) WHERE is_default = TRUE;

INSERT INTO embedders (name, dim, is_default, description) VALUES
    ('bge-m3', 1024, TRUE, 'BAAI/bge-m3, dense; 1024-dim; multilingual');


CREATE TABLE summarizers (
    name            TEXT PRIMARY KEY,
    prompt_template TEXT,
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    description     TEXT,
    deprecated_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX summarizers_one_default_idx
    ON summarizers (is_default) WHERE is_default = TRUE;

INSERT INTO summarizers (name, config, is_default, description) VALUES
    ('rake-lemma',
     '{"lemmatizer": "scispacy", "model": "en_core_sci_sm", "max_keywords": 50,
       "min_phrase_words": 1, "max_phrase_words": 4}'::jsonb,
     TRUE,
     'RAKE phrase extraction + scispacy lemmatisation');


-- ===========================================================================
-- 6. Hub tables
-- ===========================================================================

-- pdfs — one row per unique PDF; refs may share via pdf_pages range
CREATE TABLE pdfs (
    pdf_sha256   CHAR(64) PRIMARY KEY,
    content_hash CHAR(64) NOT NULL,
    page_count   INT      NOT NULL,
    size_bytes   BIGINT   NOT NULL,
    storage_path TEXT     NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX pdfs_content_hash_idx ON pdfs (content_hash);


-- refs — identifier-free hub
CREATE TABLE refs (
    ref_id                 BIGSERIAL PRIMARY KEY,
    kind                   TEXT      NOT NULL REFERENCES kinds(slug)     ON UPDATE CASCADE,
    set_by                 TEXT      REFERENCES actors(slug)              ON UPDATE CASCADE,
    -- core metadata
    title                  TEXT      NOT NULL,
    authors                JSONB,
    year                   INT,
    -- provenance
    provider               TEXT      REFERENCES providers(slug)           ON UPDATE CASCADE,
    -- human verification
    human_verified_at      TIMESTAMPTZ,
    human_verified_by      TEXT,
    human_verified_note    TEXT,
    -- retraction tracking (this ref retracted; cited-paper retraction is derived)
    retraction_status      TEXT
        CHECK (retraction_status IS NULL OR
               retraction_status IN ('retracted', 'corrected', 'expression_of_concern')),
    retracted_at           TIMESTAMPTZ,
    retraction_reason      TEXT,
    retraction_url         TEXT,
    retraction_checked_at  TIMESTAMPTZ,
    -- multi-paper-per-PDF
    pdf_sha256             CHAR(64)  REFERENCES pdfs(pdf_sha256)          ON DELETE SET NULL,
    pdf_pages              INT4RANGE,
    pdf_role               TEXT
        CHECK (pdf_role IS NULL OR
               pdf_role IN ('main', 'supplement', 'appendix', 'front_matter', 'back_matter')),
    -- bookkeeping
    meta                   JSONB     NOT NULL DEFAULT '{}'::jsonb,
    deleted_at             TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX refs_kind_idx            ON refs (kind);
CREATE INDEX refs_year_idx            ON refs (year)              WHERE year IS NOT NULL;
CREATE INDEX refs_retraction_idx      ON refs (retraction_status) WHERE retraction_status IS NOT NULL;
CREATE INDEX refs_human_verified_idx  ON refs (human_verified_at) WHERE human_verified_at IS NOT NULL;
CREATE INDEX refs_alive_idx           ON refs (kind, year)        WHERE deleted_at IS NULL;
CREATE INDEX refs_pdf_sha256_idx      ON refs (pdf_sha256)        WHERE pdf_sha256 IS NOT NULL;
CREATE INDEX refs_provider_idx        ON refs (provider)          WHERE provider IS NOT NULL;


-- ref_identifiers — THE identifier table (see ADR 0008)
CREATE TABLE ref_identifiers (
    id_kind     TEXT NOT NULL,
        -- 'pub_id' | 'cite_key' | 'paper_id'
        --  | 'doi' | 'arxiv' | 's2' | 'pubmed' | 'openalex'
        --  | 'pdf_sha256' | 'content_hash'
    id_value    TEXT NOT NULL,
    ref_id      BIGINT NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    source      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id_kind, id_value)
);
CREATE INDEX ref_identifiers_ref_id_idx       ON ref_identifiers (ref_id);
CREATE INDEX ref_identifiers_cite_key_trgm_idx
    ON ref_identifiers USING GIN (id_value gin_trgm_ops)
    WHERE id_kind = 'cite_key';


-- ===========================================================================
-- 7. Chunks (content layer; cards at ord<0, body at ord>=0)
-- ===========================================================================
CREATE TABLE chunks (
    chunk_id      BIGSERIAL PRIMARY KEY,
    ref_id        BIGINT  NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    set_by        TEXT    REFERENCES actors(slug) ON UPDATE CASCADE,
    ord           INT     NOT NULL,
    chunk_kind    TEXT    NOT NULL REFERENCES chunk_kinds(slug) ON UPDATE CASCADE,
    text          TEXT    NOT NULL,
    block_ids     BIGINT[] NOT NULL DEFAULT '{}',
    token_count   INT,
    section_path  TEXT[]  NOT NULL DEFAULT '{}',
    page_first    INT,
    page_last     INT,
    meta          JSONB   NOT NULL DEFAULT '{}'::jsonb,
    tsv           TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ref_id, ord),
    CHECK (
        (ord <  0 AND chunk_kind LIKE 'card_%') OR
        (ord >= 0 AND chunk_kind NOT LIKE 'card_%')
    ),
    CHECK (page_first IS NULL OR page_last IS NULL OR page_first <= page_last)
);
CREATE INDEX chunks_ref_id_idx       ON chunks (ref_id);
CREATE INDEX chunks_chunk_kind_idx   ON chunks (chunk_kind);
CREATE INDEX chunks_section_path_idx ON chunks USING GIN (section_path);
CREATE INDEX chunks_tsv_idx          ON chunks USING GIN (tsv);
CREATE INDEX chunks_cards_idx        ON chunks (ref_id, ord) WHERE ord < 0;


-- ===========================================================================
-- 8. Derived artifacts (embeddings, summaries) — see ADR 0007
-- ===========================================================================

-- chunk_embeddings: many vectors per chunk (one per registered embedder).
-- vector dim today is locked to embedders.dim for the is_default embedder
-- (1024 for bge-m3). When a different-dim embedder is registered, this
-- table will be partitioned by embedder; defer.
CREATE TABLE chunk_embeddings (
    chunk_id    BIGINT NOT NULL REFERENCES chunks(chunk_id)   ON DELETE CASCADE,
    embedder    TEXT   NOT NULL REFERENCES embedders(name)    ON UPDATE CASCADE,
    vector      vector(1024),
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
-- HNSW vector index is created on-demand by the application layer (see
-- storage-v2.md "Search strategy"). Building it here would index zero
-- rows and require rebuild after first ingest.


-- chunk_summaries: many summaries per chunk (one per registered summarizer).
CREATE TABLE chunk_summaries (
    chunk_id    BIGINT NOT NULL REFERENCES chunks(chunk_id)     ON DELETE CASCADE,
    summarizer  TEXT   NOT NULL REFERENCES summarizers(name)    ON UPDATE CASCADE,
    text        TEXT,
    prompt_hash CHAR(64),
    token_count INT,
    status      TEXT   NOT NULL DEFAULT 'ok'
        CHECK (status IN ('ok', 'failed')),
    attempts    INT    NOT NULL DEFAULT 1,
    last_error  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, summarizer)
);
CREATE INDEX chunk_summaries_failed_idx
    ON chunk_summaries (chunk_id, summarizer)
    WHERE status = 'failed';


-- ===========================================================================
-- 9. Graph (links between refs and/or chunks)
-- ===========================================================================
CREATE TABLE links (
    link_id       BIGSERIAL PRIMARY KEY,
    src_ref_id    BIGINT  NOT NULL REFERENCES refs(ref_id)     ON DELETE CASCADE,
    src_chunk_id  BIGINT  REFERENCES chunks(chunk_id)          ON DELETE CASCADE,
    dst_ref_id    BIGINT  NOT NULL REFERENCES refs(ref_id)     ON DELETE CASCADE,
    dst_chunk_id  BIGINT  REFERENCES chunks(chunk_id)          ON DELETE CASCADE,
    relation      TEXT    NOT NULL REFERENCES relations(slug)  ON UPDATE CASCADE,
    set_by        TEXT    NOT NULL REFERENCES actors(slug)     ON UPDATE CASCADE,
    meta          JSONB   NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- No self-loop at the same precision: same ref AND same chunk-id-ness.
    -- IS NOT DISTINCT FROM treats NULL = NULL (both ref-level, both same ref → loop).
    CHECK (NOT (src_ref_id = dst_ref_id
                AND src_chunk_id IS NOT DISTINCT FROM dst_chunk_id))
);
-- Dedup endpoints + relation. NULLS NOT DISTINCT means (1, NULL, 2, NULL, 'cites')
-- collides with itself, preventing duplicate ref-level links.
CREATE UNIQUE INDEX links_endpoints_relation_idx
    ON links (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation)
    NULLS NOT DISTINCT;
CREATE INDEX links_src_ref_idx    ON links (src_ref_id);
CREATE INDEX links_dst_ref_idx    ON links (dst_ref_id);
CREATE INDEX links_src_chunk_idx  ON links (src_chunk_id) WHERE src_chunk_id IS NOT NULL;
CREATE INDEX links_dst_chunk_idx  ON links (dst_chunk_id) WHERE dst_chunk_id IS NOT NULL;
CREATE INDEX links_relation_idx   ON links (relation);


-- ===========================================================================
-- 10. Tags (one normalised tag space; polymorphic via per-target tables)
-- ===========================================================================
CREATE TABLE tags (
    tag_id      BIGSERIAL PRIMARY KEY,
    namespace   TEXT NOT NULL,    -- 'SRC' | 'CACHE' | 'STATUS' | 'PRIO' | 'RETRACTION' | 'ASPECT' | …
    value       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (namespace, value),
    CHECK (namespace = upper(namespace) AND namespace <> ''),
    CHECK (value <> '')
);
CREATE INDEX tags_namespace_idx ON tags (namespace);


CREATE TABLE ref_tags (
    ref_id      BIGINT NOT NULL REFERENCES refs(ref_id)   ON DELETE CASCADE,
    tag_id      BIGINT NOT NULL REFERENCES tags(tag_id)   ON DELETE CASCADE,
    set_by      TEXT   REFERENCES actors(slug)            ON UPDATE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, tag_id)
);
CREATE INDEX ref_tags_tag_id_idx ON ref_tags (tag_id);


CREATE TABLE chunk_tags (
    chunk_id    BIGINT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    tag_id      BIGINT NOT NULL REFERENCES tags(tag_id)     ON DELETE CASCADE,
    set_by      TEXT   REFERENCES actors(slug)              ON UPDATE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, tag_id)
);
CREATE INDEX chunk_tags_tag_id_idx ON chunk_tags (tag_id);


-- ===========================================================================
-- 11. Views
-- ===========================================================================

-- v_refs: ergonomic access to refs with pub_id, cite_key, paper_id exposed
-- as columns. The three subqueries each hit ref_identifiers_pkey via
-- (id_kind, id_value) → fast indexed lookups.
CREATE VIEW v_refs AS
SELECT r.*,
       (SELECT id_value FROM ref_identifiers
          WHERE ref_id = r.ref_id AND id_kind = 'pub_id')   AS pub_id,
       (SELECT id_value FROM ref_identifiers
          WHERE ref_id = r.ref_id AND id_kind = 'cite_key') AS cite_key,
       (SELECT id_value FROM ref_identifiers
          WHERE ref_id = r.ref_id AND id_kind = 'paper_id') AS paper_id
FROM refs r;


-- v_ref_tags_all: tags directly on a ref UNION tags on its chunks.
-- `via` discriminator: 'direct' for ref_tags rows, 'chunk' for chunk-derived.
CREATE VIEW v_ref_tags_all AS
SELECT rt.ref_id,
       t.tag_id,
       t.namespace,
       t.value,
       'direct'::TEXT AS via,
       NULL::BIGINT   AS chunk_id,
       rt.set_by,
       rt.created_at
FROM ref_tags rt
JOIN tags t USING (tag_id)
UNION ALL
SELECT c.ref_id,
       t.tag_id,
       t.namespace,
       t.value,
       'chunk'::TEXT AS via,
       c.chunk_id,
       ct.set_by,
       ct.created_at
FROM chunk_tags ct
JOIN chunks c USING (chunk_id)
JOIN tags   t USING (tag_id);


-- v_chunk_tags_all: tags directly on a chunk UNION tags on its parent ref.
-- `via` discriminator: 'direct' for chunk_tags rows, 'ref' for parent-derived.
CREATE VIEW v_chunk_tags_all AS
SELECT ct.chunk_id,
       t.tag_id,
       t.namespace,
       t.value,
       'direct'::TEXT AS via,
       ct.set_by,
       ct.created_at
FROM chunk_tags ct
JOIN tags t USING (tag_id)
UNION ALL
SELECT c.chunk_id,
       t.tag_id,
       t.namespace,
       t.value,
       'ref'::TEXT AS via,
       rt.set_by,
       rt.created_at
FROM ref_tags rt
JOIN tags   t USING (tag_id)
JOIN chunks c USING (ref_id);


-- ============================================================================
-- End of 0001_initial.sql
-- ============================================================================
