-- ===========================================================================
-- Precis V2 — initial schema (0001_initial)
--
-- Design notes:
--   * Reference tables are FK targets for all closed vocabularies. Seeds
--     ship with this migration; future additions = new migration.
--   * `refs` is the hub. Everything (blocks, links, tags, cache_state) hangs
--     off ref_id with ON DELETE CASCADE.
--   * Soft-delete via refs.deleted_at; live indexes filter on it.
--   * Tags split by namespace (closed / flag / open) so each FKs into its
--     own controlled vocabulary.
--   * Block `pos` is renumberable; `slug` is the permanent citation handle.
--   * `system` table holds global state that must travel with the data
--     (embedding model, dim, schema epoch).
--   * Hybrid search: tsvector (GIN) + pgvector embedding (HNSW) +
--     pg_trgm on slug for fuzzy lookup.
--   * Migration runner wraps this file in a transaction and records the
--     version into _migrations on success. Forward-only.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;


-- ---------------------------------------------------------------------------
-- Migrations ledger
-- ---------------------------------------------------------------------------
CREATE TABLE _migrations (
    version    TEXT         PRIMARY KEY,
    applied_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    checksum   TEXT         NOT NULL
);


-- ---------------------------------------------------------------------------
-- Reference tables (closed vocabularies, FK targets)
-- ---------------------------------------------------------------------------

CREATE TABLE actors (
    slug        TEXT PRIMARY KEY,
    description TEXT
);

INSERT INTO actors (slug, description) VALUES
    ('agent',  'LLM-mediated tool call'),
    ('user',   'Direct human invocation (CLI, ops)'),
    ('system', 'Server-side automation: sweeps, derived state, defaults');


CREATE TABLE kinds (
    slug        TEXT    PRIMARY KEY,
    is_numeric  BOOLEAN NOT NULL,    -- t = public id is refs.id; f = refs.slug
    title       TEXT    NOT NULL,
    description TEXT
);

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    -- canonical, slug-addressed
    ('paper',   FALSE, 'Paper',        'Research paper, addressed by slug'),
    ('book',    FALSE, 'Book',         'Book, addressed by slug'),
    ('oracle',  FALSE, 'Oracle',       'Oracle / authority node'),
    ('conv',    FALSE, 'Conversation', 'Conversation transcript'),
    ('skill',   FALSE, 'Skill',        'Agent skill document'),
    ('quest',   FALSE, 'Quest',        'Request queue item'),
    -- ephemeral, numeric-id
    ('todo',    TRUE,  'Todo',         'Task / action item'),
    ('memory',  TRUE,  'Memory',       'Note, decision, idea, claim'),
    ('gripe',   TRUE,  'Gripe',        'Informal log entry'),
    ('fc',      TRUE,  'Flashcard',    'Spaced-repetition flashcard'),
    -- paid-tool caches (slug-addressed, hash-derived)
    ('web',     FALSE, 'Web query',    'Cached web/research/think query'),
    ('youtube', FALSE, 'YouTube',      'Cached YouTube transcript'),
    ('math',    FALSE, 'Math result',  'Cached Wolfram math result');


CREATE TABLE relations (
    slug         TEXT    PRIMARY KEY,
    symmetric    BOOLEAN NOT NULL DEFAULT FALSE,
    inverse_slug TEXT,                  -- label only; app enforces inversion
    description  TEXT
);

INSERT INTO relations (slug, symmetric, inverse_slug, description) VALUES
    ('related-to',      TRUE,  NULL,              'Symmetric association'),
    ('blocks',          FALSE, 'blocked-by',      'Source blocks target'),
    ('blocked-by',      FALSE, 'blocks',          'Source is blocked by target'),
    ('contradicts',     FALSE, 'contradicted-by', 'Source contradicts target'),
    ('contradicted-by', FALSE, 'contradicts',     'Source is contradicted by target');


CREATE TABLE tag_prefixes (
    prefix       TEXT PRIMARY KEY,
    writable_by  TEXT NOT NULL REFERENCES actors(slug) ON UPDATE CASCADE,
    description  TEXT
);

INSERT INTO tag_prefixes (prefix, writable_by, description) VALUES
    ('SRC',        'system', 'Provenance — which provider supplied data'),
    ('CACHE',      'system', 'Cache state — usually derived, may be set'),
    ('DENSITY',    'system', 'Block density bucket — set by sweep'),
    ('STATUS',     'agent',  'Work status — open, doing, done, blocked, ...'),
    ('PRIO',       'agent',  'Priority — high, med, low'),
    ('CONFIDENCE', 'agent',  'Claim confidence — high, med, low');


CREATE TABLE flag_names (
    name        TEXT PRIMARY KEY,
    description TEXT
);

INSERT INTO flag_names (name, description) VALUES
    ('pinned',  'Exempt from cache freshness sweep'),
    ('urgent',  'Agent-set urgency marker'),
    ('private', 'Suppress from broad cross-corpus searches');


CREATE TABLE providers (
    slug        TEXT PRIMARY KEY,
    description TEXT
);

INSERT INTO providers (slug, description) VALUES
    -- paid web tools
    ('perplexity', 'Perplexity (web/research/think)'),
    ('wolfram',    'Wolfram Alpha math'),
    ('youtube',    'YouTube transcript'),
    -- paper sources
    ('arxiv',      'arXiv preprint server'),
    ('crossref',   'Crossref DOI metadata'),
    ('s2',         'Semantic Scholar'),
    ('unpaywall',  'Unpaywall OA index'),
    ('manual',     'Manually uploaded'),
    -- local
    ('local',      'Local computation / no external source');


CREATE TABLE density_levels (
    level       TEXT PRIMARY KEY,
    description TEXT
);

INSERT INTO density_levels (level, description) VALUES
    ('sparse', 'Boilerplate, references, lists — low information density'),
    ('medium', 'Typical prose'),
    ('dense',  'Findings, claims, equations — high information density');


-- ---------------------------------------------------------------------------
-- System singleton (global config that must travel with the data)
-- ---------------------------------------------------------------------------
CREATE TABLE system (
    key         TEXT         PRIMARY KEY,
    value       TEXT         NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

INSERT INTO system (key, value) VALUES
    ('embedding_model', 'BAAI/bge-m3'),
    ('embedding_dim',   '1024'),
    ('schema_epoch',    '1');


-- ---------------------------------------------------------------------------
-- Corpuses
-- ---------------------------------------------------------------------------
CREATE TABLE corpuses (
    id          BIGSERIAL    PRIMARY KEY,
    slug        TEXT         NOT NULL UNIQUE,
    title       TEXT         NOT NULL,
    meta        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- Refs — the hub
-- ---------------------------------------------------------------------------
CREATE TABLE refs (
    id          BIGSERIAL    PRIMARY KEY,
    corpus_id   BIGINT       NOT NULL REFERENCES corpuses(id)  ON DELETE RESTRICT,
    kind        TEXT         NOT NULL REFERENCES kinds(slug)   ON UPDATE CASCADE,
    slug        TEXT,                  -- NULL for numeric kinds; required for slug kinds (app-enforced)
    title       TEXT         NOT NULL,
    provider    TEXT         REFERENCES providers(slug)        ON UPDATE CASCADE,
    meta        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    title_tsv   tsvector     GENERATED ALWAYS AS (to_tsvector('english', title)) STORED,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ,           -- soft-delete sentinel
    UNIQUE (corpus_id, kind, slug)
);

CREATE INDEX refs_kind_live_idx
    ON refs (kind) WHERE deleted_at IS NULL;
CREATE INDEX refs_corpus_kind_live_idx
    ON refs (corpus_id, kind) WHERE deleted_at IS NULL;
CREATE INDEX refs_updated_idx
    ON refs (updated_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX refs_title_tsv_gin
    ON refs USING GIN (title_tsv);
CREATE INDEX refs_slug_trgm
    ON refs USING GIN (slug gin_trgm_ops) WHERE slug IS NOT NULL;
CREATE INDEX refs_provider_idx
    ON refs (provider) WHERE provider IS NOT NULL;


-- ---------------------------------------------------------------------------
-- Blocks — content chunks
--   pos: 0-based, renumberable on re-ingest
--   slug: stable short handle, citation target (e.g. 'PLXDX')
--   embedding: dim must match system.embedding_dim
-- ---------------------------------------------------------------------------
CREATE TABLE blocks (
    id          BIGSERIAL    PRIMARY KEY,
    ref_id      BIGINT       NOT NULL REFERENCES refs(id) ON DELETE CASCADE,
    pos         INT          NOT NULL,
    slug        TEXT,
    text        TEXT         NOT NULL,
    token_count INT,
    embedding   vector(1024),                                             -- bge-m3 default
    density     TEXT         REFERENCES density_levels(level) ON UPDATE CASCADE,
    meta        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    tsv         tsvector     GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (ref_id, pos),
    UNIQUE (ref_id, slug)
);

CREATE INDEX blocks_ref_pos_idx
    ON blocks (ref_id, pos);
CREATE INDEX blocks_tsv_gin
    ON blocks USING GIN (tsv);
CREATE INDEX blocks_embedding_hnsw
    ON blocks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX blocks_density_idx
    ON blocks (density) WHERE density IS NOT NULL;


-- ---------------------------------------------------------------------------
-- Links — graph edges between refs / blocks
--   Stored in natural direction once; readers query bidirectionally.
--   ref-level: src_pos / dst_pos NULL.
--   block-level: src_pos / dst_pos = blocks.pos.
-- ---------------------------------------------------------------------------
CREATE TABLE links (
    id          BIGSERIAL    PRIMARY KEY,
    src_ref_id  BIGINT       NOT NULL REFERENCES refs(id) ON DELETE CASCADE,
    src_pos     INT,
    dst_ref_id  BIGINT       NOT NULL REFERENCES refs(id) ON DELETE CASCADE,
    dst_pos     INT,
    relation    TEXT         NOT NULL REFERENCES relations(slug) ON UPDATE CASCADE,
    set_by      TEXT         NOT NULL REFERENCES actors(slug)    ON UPDATE CASCADE,
    meta        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (src_ref_id, src_pos, dst_ref_id, dst_pos, relation),
    -- Prevent identity self-loops (same ref AND same pos including both NULL).
    -- Same-ref different-pos links remain allowed.
    CHECK (NOT (src_ref_id = dst_ref_id
                AND src_pos IS NOT DISTINCT FROM dst_pos))
);

CREATE INDEX links_src_idx      ON links (src_ref_id);
CREATE INDEX links_dst_idx      ON links (dst_ref_id);
CREATE INDEX links_relation_idx ON links (relation);


-- ---------------------------------------------------------------------------
-- Tags — three tables, one per namespace
--   Conceptually one annotation space; split for clean FK enforcement.
--   pos NULL = ref-level, INT = block-level (must match a blocks.pos).
-- ---------------------------------------------------------------------------

CREATE TABLE ref_closed_tags (
    ref_id      BIGINT       NOT NULL REFERENCES refs(id)            ON DELETE CASCADE,
    pos         INT,
    prefix      TEXT         NOT NULL REFERENCES tag_prefixes(prefix) ON UPDATE CASCADE,
    value       TEXT         NOT NULL,
    set_by      TEXT         NOT NULL REFERENCES actors(slug)         ON UPDATE CASCADE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, pos, prefix, value)
);
CREATE INDEX ref_closed_tags_prefix_value_idx ON ref_closed_tags (prefix, value);


CREATE TABLE ref_flags (
    ref_id      BIGINT       NOT NULL REFERENCES refs(id)         ON DELETE CASCADE,
    pos         INT,
    name        TEXT         NOT NULL REFERENCES flag_names(name) ON UPDATE CASCADE,
    set_by      TEXT         NOT NULL REFERENCES actors(slug)     ON UPDATE CASCADE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, pos, name)
);
CREATE INDEX ref_flags_name_idx ON ref_flags (name);


CREATE TABLE ref_open_tags (
    ref_id      BIGINT       NOT NULL REFERENCES refs(id)     ON DELETE CASCADE,
    pos         INT,
    value       TEXT         NOT NULL CHECK (value = lower(value) AND value <> ''),
    set_by      TEXT         NOT NULL REFERENCES actors(slug) ON UPDATE CASCADE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, pos, value)
);
CREATE INDEX ref_open_tags_value_idx ON ref_open_tags (value);


-- ---------------------------------------------------------------------------
-- Cache state — paid-tool freshness metadata
--   One row per cached ref; ref kind in {'web','youtube','math',...}.
--   request_hash is deterministic on normalized request → idempotent caching.
--   Freshness derived on read from fresh_until vs now() — no maintenance sweep
--   needed for correctness; sweeps only drive notifications.
--   fresh_until NULL = pinned (exempt from expiry).
-- ---------------------------------------------------------------------------
CREATE TABLE cache_state (
    ref_id        BIGINT       PRIMARY KEY REFERENCES refs(id) ON DELETE CASCADE,
    provider      TEXT         NOT NULL REFERENCES providers(slug) ON UPDATE CASCADE,
    request_hash  TEXT         NOT NULL,
    model         TEXT,
    fetched_at    TIMESTAMPTZ  NOT NULL,
    fresh_until   TIMESTAMPTZ,                                            -- NULL = pinned
    cost_usd      NUMERIC(10,6),
    meta          JSONB        NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX cache_state_provider_hash_idx
    ON cache_state (provider, request_hash);
CREATE INDEX cache_state_fresh_until_idx
    ON cache_state (fresh_until) WHERE fresh_until IS NOT NULL;
CREATE INDEX cache_state_provider_idx
    ON cache_state (provider);


-- ---------------------------------------------------------------------------
-- Convenience views
-- ---------------------------------------------------------------------------

-- All tags on a ref/block, unified across the three tag tables.
CREATE VIEW ref_tags AS
    SELECT ref_id, pos,
           'closed'::TEXT AS namespace,
           prefix || ':' || value AS tag,
           prefix, value AS detail,
           set_by, created_at
    FROM   ref_closed_tags
    UNION ALL
    SELECT ref_id, pos,
           'flag'::TEXT AS namespace,
           name AS tag,
           NULL::TEXT AS prefix, name AS detail,
           set_by, created_at
    FROM   ref_flags
    UNION ALL
    SELECT ref_id, pos,
           'open'::TEXT AS namespace,
           value AS tag,
           NULL::TEXT AS prefix, value AS detail,
           set_by, created_at
    FROM   ref_open_tags;

-- Cache freshness derived on read (no materialization).
CREATE VIEW cache_freshness AS
    SELECT ref_id,
           provider,
           fetched_at,
           fresh_until,
           CASE
             WHEN fresh_until IS NULL                        THEN 'pinned'
             WHEN fresh_until > now()                        THEN 'fresh'
             WHEN fresh_until > now() - INTERVAL '7 days'    THEN 'stale'
             ELSE                                                 'expired'
           END AS state
    FROM cache_state;


-- ===========================================================================
-- End of 0001_initial.sql
-- ===========================================================================
