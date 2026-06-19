-- migrations/baseline/schema.sql — generated baseline snapshot.
--
-- DO NOT EDIT BY HAND. Regenerate with `precis db dump-schema`
-- (or `scripts/bump`, which does it at every version bump).
--
-- Baked-in migration head: 0028_normalize_owner_identity_tag
--
-- This is the migration chain compiled to one file: a fresh
-- `precis migrate` loads this instead of replaying every numbered
-- migration, then applies any migrations added since this snapshot
-- as a normal tail. The numbered migrations stay sealed in the tree
-- as the upgrade path for existing databases (ADR 0031). This is NOT
-- a greenfield — nothing is deleted.
--
-- Extensions (pg_dump --schema=public omits them):
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE SCHEMA IF NOT EXISTS public;

--
-- PostgreSQL database dump
--


-- Dumped from database version 17.9 (Homebrew)
-- Dumped by pg_dump version 17.9 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA IF NOT EXISTS public;


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: bump_salience(bigint[]); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.bump_salience(ids bigint[]) RETURNS void
    LANGUAGE sql
    AS $$
    UPDATE chunks SET last_seen = now(), accesses = accesses + 1
    WHERE chunk_id = ANY(ids);
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: _migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public._migrations (
    version text NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    checksum text NOT NULL,
    plugin text DEFAULT 'precis'::text NOT NULL
);


--
-- Name: actors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.actors (
    slug text NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: app_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_state (
    key text NOT NULL,
    value text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: artifact_kinds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.artifact_kinds (
    slug text NOT NULL,
    target text NOT NULL,
    storage text NOT NULL,
    output_table text NOT NULL,
    description text,
    deprecated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT artifact_kinds_storage_check CHECK ((storage = ANY (ARRAY['typed'::text, 'untyped'::text]))),
    CONSTRAINT artifact_kinds_target_check CHECK ((target = ANY (ARRAY['chunk'::text, 'ref'::text, 'link'::text, 'pdf'::text, 'tag'::text])))
);


--
-- Name: cache_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cache_state (
    ref_id bigint NOT NULL,
    provider text NOT NULL,
    request_hash text NOT NULL,
    model text,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    fresh_until timestamp with time zone,
    cost_usd numeric,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: chunk_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk_embeddings (
    chunk_id bigint NOT NULL,
    embedder text NOT NULL,
    vector public.vector(1024),
    status text DEFAULT 'ok'::text NOT NULL,
    attempts integer DEFAULT 1 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chunk_embeddings_status_check CHECK ((status = ANY (ARRAY['ok'::text, 'failed'::text])))
);


--
-- Name: chunk_kinds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk_kinds (
    slug text NOT NULL,
    is_card boolean DEFAULT false NOT NULL,
    description text,
    deprecated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chunk_summaries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk_summaries (
    chunk_id bigint NOT NULL,
    summarizer text NOT NULL,
    text text,
    prompt_hash character(64),
    token_count integer,
    status text DEFAULT 'ok'::text NOT NULL,
    attempts integer DEFAULT 1 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chunk_summaries_status_check CHECK ((status = ANY (ARRAY['ok'::text, 'failed'::text])))
);


--
-- Name: chunk_tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk_tags (
    chunk_id bigint NOT NULL,
    tag_id bigint NOT NULL,
    set_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunks (
    chunk_id bigint NOT NULL,
    ref_id bigint NOT NULL,
    set_by text,
    ord integer NOT NULL,
    chunk_kind text NOT NULL,
    text text NOT NULL,
    block_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    token_count integer,
    section_path text[] DEFAULT '{}'::text[] NOT NULL,
    page_first integer,
    page_last integer,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, text)) STORED,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    numerics text[] DEFAULT '{}'::text[] NOT NULL,
    keywords text[],
    keywords_meta jsonb,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_dreamt timestamp with time zone DEFAULT now() NOT NULL,
    accesses integer DEFAULT 0 NOT NULL,
    last_watched timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chunks_check CHECK ((((ord < 0) AND (chunk_kind ~~ 'card_%'::text)) OR ((ord >= 0) AND (chunk_kind !~~ 'card_%'::text)))),
    CONSTRAINT chunks_check1 CHECK (((page_first IS NULL) OR (page_last IS NULL) OR (page_first <= page_last)))
);


--
-- Name: chunks_chunk_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chunks_chunk_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chunks_chunk_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chunks_chunk_id_seq OWNED BY public.chunks.chunk_id;


--
-- Name: claude_quota_snapshot; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.claude_quota_snapshot (
    scope text NOT NULL,
    ts timestamp with time zone NOT NULL,
    data jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: cluster_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cluster_assignments (
    run_id bigint NOT NULL,
    chunk_id bigint NOT NULL,
    ref_id bigint NOT NULL,
    leaf_path text NOT NULL
);


--
-- Name: cluster_cells; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cluster_cells (
    run_id bigint NOT NULL,
    path text NOT NULL,
    parent_path text,
    depth integer NOT NULL,
    grid_row integer NOT NULL,
    grid_col integer NOT NULL,
    is_leaf boolean DEFAULT true NOT NULL,
    n_chunks integer DEFAULT 0 NOT NULL,
    n_refs integer DEFAULT 0 NOT NULL,
    words jsonb DEFAULT '[]'::jsonb NOT NULL,
    centroid public.vector(1024)
);


--
-- Name: cluster_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cluster_runs (
    run_id bigint NOT NULL,
    scope text NOT NULL,
    status text DEFAULT 'building'::text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    n_vectors integer DEFAULT 0 NOT NULL,
    note text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone
);


--
-- Name: cluster_runs_run_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.cluster_runs ALTER COLUMN run_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cluster_runs_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: dream_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dream_log (
    attempt_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    outcome text NOT NULL,
    behaviors text[],
    seed_clusters jsonb,
    result_ref_ids bigint[],
    turns integer,
    tool_calls integer,
    model text,
    cost_usd double precision,
    summary jsonb
);


--
-- Name: dream_log_attempt_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.dream_log_attempt_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: dream_log_attempt_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.dream_log_attempt_id_seq OWNED BY public.dream_log.attempt_id;


--
-- Name: dream_transcripts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dream_transcripts (
    attempt_id bigint NOT NULL,
    transcript jsonb NOT NULL
);


--
-- Name: embedders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.embedders (
    name text NOT NULL,
    dim integer NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    description text,
    deprecated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: host_heartbeat; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.host_heartbeat (
    host text NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    temp_c double precision,
    load1 double precision,
    load5 double precision,
    load15 double precision,
    meta jsonb
);


--
-- Name: kind_provider; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kind_provider (
    slug text NOT NULL,
    host text NOT NULL,
    process text NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: kinds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kinds (
    slug text NOT NULL,
    is_numeric boolean DEFAULT false NOT NULL,
    title text NOT NULL,
    description text,
    deprecated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.links (
    link_id bigint NOT NULL,
    src_ref_id bigint NOT NULL,
    src_chunk_id bigint,
    dst_ref_id bigint NOT NULL,
    dst_chunk_id bigint,
    relation text NOT NULL,
    set_by text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT links_check CHECK ((NOT ((src_ref_id = dst_ref_id) AND (NOT (src_chunk_id IS DISTINCT FROM dst_chunk_id)))))
);


--
-- Name: links_link_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.links_link_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: links_link_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.links_link_id_seq OWNED BY public.links.link_id;


--
-- Name: patent_watches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.patent_watches (
    id bigint NOT NULL,
    name text NOT NULL,
    cql text NOT NULL,
    interval_s integer NOT NULL,
    max_per_pass integer,
    last_run_at timestamp with time zone,
    last_seen_pn text[],
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text NOT NULL,
    CONSTRAINT patent_watches_interval_s_check CHECK ((interval_s > 0)),
    CONSTRAINT patent_watches_max_per_pass_check CHECK (((max_per_pass IS NULL) OR (max_per_pass > 0)))
);


--
-- Name: patent_watches_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.patent_watches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: patent_watches_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.patent_watches_id_seq OWNED BY public.patent_watches.id;


--
-- Name: pdfs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pdfs (
    pdf_sha256 character(64) NOT NULL,
    content_hash character(64) NOT NULL,
    page_count integer NOT NULL,
    size_bytes bigint NOT NULL,
    storage_path text NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: provenance_rw_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provenance_rw_cache (
    record_id bigint NOT NULL,
    paper_doi text NOT NULL,
    notice_doi text,
    notice_nature text NOT NULL,
    reasons text[] DEFAULT '{}'::text[] NOT NULL,
    retraction_date date,
    paper_title text,
    journal text,
    raw jsonb DEFAULT '{}'::jsonb NOT NULL,
    synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: provenance_rw_sync; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provenance_rw_sync (
    source_url text NOT NULL,
    last_full_sync_at timestamp with time zone,
    last_row_count integer,
    last_status text,
    last_error text
);


--
-- Name: providers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.providers (
    slug text NOT NULL,
    description text,
    deprecated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ref_artifacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ref_artifacts (
    ref_id bigint NOT NULL,
    artifact text NOT NULL,
    payload jsonb,
    status text DEFAULT 'ok'::text NOT NULL,
    attempts integer DEFAULT 1 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ref_artifacts_status_check CHECK ((status = ANY (ARRAY['ok'::text, 'failed'::text])))
);


--
-- Name: ref_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ref_events (
    event_id bigint NOT NULL,
    ref_id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    source text NOT NULL,
    event text NOT NULL,
    payload jsonb,
    duration_ms integer,
    cost_usd numeric
);


--
-- Name: ref_events_event_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ref_events_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ref_events_event_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ref_events_event_id_seq OWNED BY public.ref_events.event_id;


--
-- Name: ref_identifiers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ref_identifiers (
    id_kind text NOT NULL,
    id_value text NOT NULL,
    ref_id bigint NOT NULL,
    source text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ref_tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ref_tags (
    ref_id bigint NOT NULL,
    tag_id bigint NOT NULL,
    set_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone
);


--
-- Name: refs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.refs (
    ref_id bigint NOT NULL,
    kind text NOT NULL,
    set_by text,
    title text NOT NULL,
    authors jsonb,
    year integer,
    provider text,
    human_verified_at timestamp with time zone,
    human_verified_by text,
    human_verified_note text,
    retraction_status text,
    retracted_at timestamp with time zone,
    retraction_reason text,
    retraction_url text,
    retraction_checked_at timestamp with time zone,
    pdf_sha256 character(64),
    pdf_pages int4range,
    pdf_role text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    auto_refresh_days integer,
    refreshed_at timestamp with time zone,
    parent_id bigint,
    prio smallint,
    CONSTRAINT refs_pdf_role_check CHECK (((pdf_role IS NULL) OR (pdf_role = ANY (ARRAY['main'::text, 'supplement'::text, 'appendix'::text, 'front_matter'::text, 'back_matter'::text])))),
    CONSTRAINT refs_prio_check CHECK (((prio IS NULL) OR ((prio >= 1) AND (prio <= 10)))),
    CONSTRAINT refs_retraction_status_check CHECK (((retraction_status IS NULL) OR (retraction_status = ANY (ARRAY['retracted'::text, 'corrected'::text, 'expression_of_concern'::text]))))
);


--
-- Name: refs_ref_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.refs_ref_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: refs_ref_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.refs_ref_id_seq OWNED BY public.refs.ref_id;


--
-- Name: relations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.relations (
    slug text NOT NULL,
    is_symmetric boolean DEFAULT false NOT NULL,
    inverse_slug text,
    description text,
    deprecated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: summarizers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.summarizers (
    name text NOT NULL,
    prompt_template text,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    description text,
    deprecated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tag_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tag_embeddings (
    namespace text NOT NULL,
    value text NOT NULL,
    vector public.vector(1024),
    version integer DEFAULT 1 NOT NULL,
    embedder text NOT NULL,
    embedded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tags (
    tag_id bigint NOT NULL,
    namespace text NOT NULL,
    value text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tags_namespace_check CHECK (((namespace = upper(namespace)) AND (namespace <> ''::text))),
    CONSTRAINT tags_value_check CHECK ((value <> ''::text))
);


--
-- Name: tags_tag_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tags_tag_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: tags_tag_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tags_tag_id_seq OWNED BY public.tags.tag_id;


--
-- Name: v_chunk_tags_all; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_chunk_tags_all AS
 SELECT ct.chunk_id,
    t.tag_id,
    t.namespace,
    t.value,
    'direct'::text AS via,
    ct.set_by,
    ct.created_at
   FROM (public.chunk_tags ct
     JOIN public.tags t USING (tag_id))
UNION ALL
 SELECT c.chunk_id,
    t.tag_id,
    t.namespace,
    t.value,
    'ref'::text AS via,
    rt.set_by,
    rt.created_at
   FROM ((public.ref_tags rt
     JOIN public.tags t USING (tag_id))
     JOIN public.chunks c USING (ref_id));


--
-- Name: v_ref_tags_all; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_ref_tags_all AS
 SELECT rt.ref_id,
    t.tag_id,
    t.namespace,
    t.value,
    'direct'::text AS via,
    NULL::bigint AS chunk_id,
    rt.set_by,
    rt.created_at
   FROM (public.ref_tags rt
     JOIN public.tags t USING (tag_id))
UNION ALL
 SELECT c.ref_id,
    t.tag_id,
    t.namespace,
    t.value,
    'chunk'::text AS via,
    c.chunk_id,
    ct.set_by,
    ct.created_at
   FROM ((public.chunk_tags ct
     JOIN public.chunks c USING (chunk_id))
     JOIN public.tags t USING (tag_id));


--
-- Name: v_refs; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_refs AS
 SELECT ref_id,
    kind,
    set_by,
    title,
    authors,
    year,
    provider,
    human_verified_at,
    human_verified_by,
    human_verified_note,
    retraction_status,
    retracted_at,
    retraction_reason,
    retraction_url,
    retraction_checked_at,
    pdf_sha256,
    pdf_pages,
    pdf_role,
    meta,
    deleted_at,
    created_at,
    updated_at,
    ( SELECT ref_identifiers.id_value
           FROM public.ref_identifiers
          WHERE ((ref_identifiers.ref_id = r.ref_id) AND (ref_identifiers.id_kind = 'pub_id'::text))) AS pub_id,
    ( SELECT ref_identifiers.id_value
           FROM public.ref_identifiers
          WHERE ((ref_identifiers.ref_id = r.ref_id) AND (ref_identifiers.id_kind = 'cite_key'::text))) AS cite_key,
    ( SELECT ref_identifiers.id_value
           FROM public.ref_identifiers
          WHERE ((ref_identifiers.ref_id = r.ref_id) AND (ref_identifiers.id_kind = 'paper_id'::text))) AS paper_id
   FROM public.refs r;


--
-- Name: worker_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.worker_logs (
    log_id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    host text NOT NULL,
    process text,
    pass text,
    level text NOT NULL,
    logger text,
    message text NOT NULL,
    payload jsonb
);


--
-- Name: worker_logs_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.worker_logs_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: worker_logs_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.worker_logs_log_id_seq OWNED BY public.worker_logs.log_id;


--
-- Name: chunks chunk_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks ALTER COLUMN chunk_id SET DEFAULT nextval('public.chunks_chunk_id_seq'::regclass);


--
-- Name: dream_log attempt_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dream_log ALTER COLUMN attempt_id SET DEFAULT nextval('public.dream_log_attempt_id_seq'::regclass);


--
-- Name: links link_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.links ALTER COLUMN link_id SET DEFAULT nextval('public.links_link_id_seq'::regclass);


--
-- Name: patent_watches id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patent_watches ALTER COLUMN id SET DEFAULT nextval('public.patent_watches_id_seq'::regclass);


--
-- Name: ref_events event_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_events ALTER COLUMN event_id SET DEFAULT nextval('public.ref_events_event_id_seq'::regclass);


--
-- Name: refs ref_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refs ALTER COLUMN ref_id SET DEFAULT nextval('public.refs_ref_id_seq'::regclass);


--
-- Name: tags tag_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags ALTER COLUMN tag_id SET DEFAULT nextval('public.tags_tag_id_seq'::regclass);


--
-- Name: worker_logs log_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_logs ALTER COLUMN log_id SET DEFAULT nextval('public.worker_logs_log_id_seq'::regclass);


--
-- Name: _migrations _migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public._migrations
    ADD CONSTRAINT _migrations_pkey PRIMARY KEY (plugin, version);


--
-- Name: actors actors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.actors
    ADD CONSTRAINT actors_pkey PRIMARY KEY (slug);


--
-- Name: app_state app_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_state
    ADD CONSTRAINT app_state_pkey PRIMARY KEY (key);


--
-- Name: artifact_kinds artifact_kinds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_kinds
    ADD CONSTRAINT artifact_kinds_pkey PRIMARY KEY (slug);


--
-- Name: cache_state cache_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cache_state
    ADD CONSTRAINT cache_state_pkey PRIMARY KEY (ref_id);


--
-- Name: cache_state cache_state_provider_request_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cache_state
    ADD CONSTRAINT cache_state_provider_request_hash_key UNIQUE (provider, request_hash);


--
-- Name: chunk_embeddings chunk_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_embeddings
    ADD CONSTRAINT chunk_embeddings_pkey PRIMARY KEY (chunk_id, embedder);


--
-- Name: chunk_kinds chunk_kinds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_kinds
    ADD CONSTRAINT chunk_kinds_pkey PRIMARY KEY (slug);


--
-- Name: chunk_summaries chunk_summaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_summaries
    ADD CONSTRAINT chunk_summaries_pkey PRIMARY KEY (chunk_id, summarizer);


--
-- Name: chunk_tags chunk_tags_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_tags
    ADD CONSTRAINT chunk_tags_pkey PRIMARY KEY (chunk_id, tag_id);


--
-- Name: chunks chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_pkey PRIMARY KEY (chunk_id);


--
-- Name: chunks chunks_ref_id_ord_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_ref_id_ord_key UNIQUE (ref_id, ord);


--
-- Name: claude_quota_snapshot claude_quota_snapshot_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.claude_quota_snapshot
    ADD CONSTRAINT claude_quota_snapshot_pkey PRIMARY KEY (scope);


--
-- Name: cluster_assignments cluster_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cluster_assignments
    ADD CONSTRAINT cluster_assignments_pkey PRIMARY KEY (run_id, chunk_id);


--
-- Name: cluster_cells cluster_cells_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cluster_cells
    ADD CONSTRAINT cluster_cells_pkey PRIMARY KEY (run_id, path);


--
-- Name: cluster_runs cluster_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cluster_runs
    ADD CONSTRAINT cluster_runs_pkey PRIMARY KEY (run_id);


--
-- Name: dream_log dream_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dream_log
    ADD CONSTRAINT dream_log_pkey PRIMARY KEY (attempt_id);


--
-- Name: dream_transcripts dream_transcripts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dream_transcripts
    ADD CONSTRAINT dream_transcripts_pkey PRIMARY KEY (attempt_id);


--
-- Name: embedders embedders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.embedders
    ADD CONSTRAINT embedders_pkey PRIMARY KEY (name);


--
-- Name: host_heartbeat host_heartbeat_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.host_heartbeat
    ADD CONSTRAINT host_heartbeat_pkey PRIMARY KEY (host);


--
-- Name: kind_provider kind_provider_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kind_provider
    ADD CONSTRAINT kind_provider_pkey PRIMARY KEY (slug, host, process);


--
-- Name: kinds kinds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kinds
    ADD CONSTRAINT kinds_pkey PRIMARY KEY (slug);


--
-- Name: links links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.links
    ADD CONSTRAINT links_pkey PRIMARY KEY (link_id);


--
-- Name: patent_watches patent_watches_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patent_watches
    ADD CONSTRAINT patent_watches_name_key UNIQUE (name);


--
-- Name: patent_watches patent_watches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patent_watches
    ADD CONSTRAINT patent_watches_pkey PRIMARY KEY (id);


--
-- Name: pdfs pdfs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pdfs
    ADD CONSTRAINT pdfs_pkey PRIMARY KEY (pdf_sha256);


--
-- Name: provenance_rw_cache provenance_rw_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provenance_rw_cache
    ADD CONSTRAINT provenance_rw_cache_pkey PRIMARY KEY (record_id);


--
-- Name: provenance_rw_sync provenance_rw_sync_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provenance_rw_sync
    ADD CONSTRAINT provenance_rw_sync_pkey PRIMARY KEY (source_url);


--
-- Name: providers providers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.providers
    ADD CONSTRAINT providers_pkey PRIMARY KEY (slug);


--
-- Name: ref_artifacts ref_artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_artifacts
    ADD CONSTRAINT ref_artifacts_pkey PRIMARY KEY (ref_id, artifact);


--
-- Name: ref_events ref_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_events
    ADD CONSTRAINT ref_events_pkey PRIMARY KEY (event_id);


--
-- Name: ref_identifiers ref_identifiers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_identifiers
    ADD CONSTRAINT ref_identifiers_pkey PRIMARY KEY (id_kind, id_value);


--
-- Name: ref_tags ref_tags_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_tags
    ADD CONSTRAINT ref_tags_pkey PRIMARY KEY (ref_id, tag_id);


--
-- Name: refs refs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refs
    ADD CONSTRAINT refs_pkey PRIMARY KEY (ref_id);


--
-- Name: relations relations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relations
    ADD CONSTRAINT relations_pkey PRIMARY KEY (slug);


--
-- Name: summarizers summarizers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.summarizers
    ADD CONSTRAINT summarizers_pkey PRIMARY KEY (name);


--
-- Name: tag_embeddings tag_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_embeddings
    ADD CONSTRAINT tag_embeddings_pkey PRIMARY KEY (namespace, value);


--
-- Name: tags tags_namespace_value_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT tags_namespace_value_key UNIQUE (namespace, value);


--
-- Name: tags tags_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT tags_pkey PRIMARY KEY (tag_id);


--
-- Name: worker_logs worker_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_logs
    ADD CONSTRAINT worker_logs_pkey PRIMARY KEY (log_id);


--
-- Name: cache_state_fresh_until_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cache_state_fresh_until_idx ON public.cache_state USING btree (fresh_until) WHERE (fresh_until IS NOT NULL);


--
-- Name: cache_state_provider_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cache_state_provider_idx ON public.cache_state USING btree (provider);


--
-- Name: chunk_embeddings_failed_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_embeddings_failed_idx ON public.chunk_embeddings USING btree (chunk_id, embedder) WHERE (status = 'failed'::text);


--
-- Name: chunk_embeddings_vec_hnsw_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_embeddings_vec_hnsw_idx ON public.chunk_embeddings USING hnsw (vector public.vector_cosine_ops) WHERE ((status = 'ok'::text) AND (vector IS NOT NULL));


--
-- Name: chunk_summaries_failed_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_summaries_failed_idx ON public.chunk_summaries USING btree (chunk_id, summarizer) WHERE (status = 'failed'::text);


--
-- Name: chunk_tags_tag_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_tags_tag_id_idx ON public.chunk_tags USING btree (tag_id);


--
-- Name: chunks_cards_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_cards_idx ON public.chunks USING btree (ref_id, ord) WHERE (ord < 0);


--
-- Name: chunks_chunk_kind_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_chunk_kind_idx ON public.chunks USING btree (chunk_kind);


--
-- Name: chunks_dream_score_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_dream_score_idx ON public.chunks USING btree (((last_seen - last_dreamt)) DESC);


--
-- Name: chunks_keywords_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_keywords_gin ON public.chunks USING gin (keywords);


--
-- Name: chunks_numerics_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_numerics_idx ON public.chunks USING gin (numerics);


--
-- Name: chunks_ref_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_ref_id_idx ON public.chunks USING btree (ref_id);


--
-- Name: chunks_section_path_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_section_path_idx ON public.chunks USING gin (section_path);


--
-- Name: chunks_tsv_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_tsv_idx ON public.chunks USING gin (tsv);


--
-- Name: chunks_watch_score_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_watch_score_idx ON public.chunks USING btree (((last_seen - last_watched)) DESC);


--
-- Name: cluster_assignments_leaf_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cluster_assignments_leaf_idx ON public.cluster_assignments USING btree (run_id, leaf_path varchar_pattern_ops);


--
-- Name: cluster_assignments_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cluster_assignments_ref_idx ON public.cluster_assignments USING btree (run_id, ref_id);


--
-- Name: cluster_cells_parent_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cluster_cells_parent_idx ON public.cluster_cells USING btree (run_id, parent_path);


--
-- Name: cluster_runs_current_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cluster_runs_current_idx ON public.cluster_runs USING btree (scope, finished_at DESC) WHERE (status = 'ok'::text);


--
-- Name: dream_log_behaviors_gin_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dream_log_behaviors_gin_idx ON public.dream_log USING gin (behaviors);


--
-- Name: dream_log_outcome_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dream_log_outcome_created_idx ON public.dream_log USING btree (outcome, created_at);


--
-- Name: embedders_one_default_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX embedders_one_default_idx ON public.embedders USING btree (is_default) WHERE (is_default = true);


--
-- Name: kind_provider_slug_recent_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX kind_provider_slug_recent_idx ON public.kind_provider USING btree (slug, last_seen DESC);


--
-- Name: links_dst_chunk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX links_dst_chunk_idx ON public.links USING btree (dst_chunk_id) WHERE (dst_chunk_id IS NOT NULL);


--
-- Name: links_dst_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX links_dst_ref_idx ON public.links USING btree (dst_ref_id);


--
-- Name: links_endpoints_relation_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX links_endpoints_relation_idx ON public.links USING btree (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation) NULLS NOT DISTINCT;


--
-- Name: links_relation_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX links_relation_idx ON public.links USING btree (relation);


--
-- Name: links_src_chunk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX links_src_chunk_idx ON public.links USING btree (src_chunk_id) WHERE (src_chunk_id IS NOT NULL);


--
-- Name: links_src_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX links_src_ref_idx ON public.links USING btree (src_ref_id);


--
-- Name: patent_watches_due_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX patent_watches_due_idx ON public.patent_watches USING btree (last_run_at NULLS FIRST);


--
-- Name: pdfs_content_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pdfs_content_hash_idx ON public.pdfs USING btree (content_hash);


--
-- Name: provenance_rw_notice_doi_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX provenance_rw_notice_doi_idx ON public.provenance_rw_cache USING btree (notice_doi) WHERE (notice_doi IS NOT NULL);


--
-- Name: provenance_rw_paper_doi_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX provenance_rw_paper_doi_idx ON public.provenance_rw_cache USING btree (paper_doi);


--
-- Name: ref_artifacts_artifact_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_artifacts_artifact_idx ON public.ref_artifacts USING btree (artifact);


--
-- Name: ref_artifacts_failed_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_artifacts_failed_idx ON public.ref_artifacts USING btree (ref_id, artifact) WHERE (status = 'failed'::text);


--
-- Name: ref_events_ref_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_events_ref_id_ts_idx ON public.ref_events USING btree (ref_id, ts DESC);


--
-- Name: ref_events_source_event_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_events_source_event_ts_idx ON public.ref_events USING btree (source, event, ts DESC);


--
-- Name: ref_identifiers_cite_key_trgm_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_identifiers_cite_key_trgm_idx ON public.ref_identifiers USING gin (id_value public.gin_trgm_ops) WHERE (id_kind = 'cite_key'::text);


--
-- Name: ref_identifiers_ref_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_identifiers_ref_id_idx ON public.ref_identifiers USING btree (ref_id);


--
-- Name: ref_tags_expires_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_tags_expires_at_idx ON public.ref_tags USING btree (expires_at) WHERE (expires_at IS NOT NULL);


--
-- Name: ref_tags_tag_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_tags_tag_id_idx ON public.ref_tags USING btree (tag_id);


--
-- Name: refs_alive_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_alive_idx ON public.refs USING btree (kind, year) WHERE (deleted_at IS NULL);


--
-- Name: refs_auto_refresh_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_auto_refresh_idx ON public.refs USING btree (auto_refresh_days, refreshed_at) WHERE (auto_refresh_days IS NOT NULL);


--
-- Name: refs_human_verified_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_human_verified_idx ON public.refs USING btree (human_verified_at) WHERE (human_verified_at IS NOT NULL);


--
-- Name: refs_kind_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_kind_idx ON public.refs USING btree (kind);


--
-- Name: refs_parent_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_parent_id_idx ON public.refs USING btree (parent_id) WHERE (parent_id IS NOT NULL);


--
-- Name: refs_pdf_sha256_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_pdf_sha256_idx ON public.refs USING btree (pdf_sha256) WHERE (pdf_sha256 IS NOT NULL);


--
-- Name: refs_prio_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_prio_idx ON public.refs USING btree (prio) WHERE (prio IS NOT NULL);


--
-- Name: refs_provider_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_provider_idx ON public.refs USING btree (provider) WHERE (provider IS NOT NULL);


--
-- Name: refs_retraction_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_retraction_idx ON public.refs USING btree (retraction_status) WHERE (retraction_status IS NOT NULL);


--
-- Name: refs_year_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_year_idx ON public.refs USING btree (year) WHERE (year IS NOT NULL);


--
-- Name: summarizers_one_default_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX summarizers_one_default_idx ON public.summarizers USING btree (is_default) WHERE (is_default = true);


--
-- Name: tag_embeddings_vector_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tag_embeddings_vector_hnsw ON public.tag_embeddings USING hnsw (vector public.vector_cosine_ops);


--
-- Name: tags_namespace_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tags_namespace_idx ON public.tags USING btree (namespace);


--
-- Name: worker_logs_host_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX worker_logs_host_ts_idx ON public.worker_logs USING btree (host, ts DESC);


--
-- Name: worker_logs_level_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX worker_logs_level_ts_idx ON public.worker_logs USING btree (level, ts DESC) WHERE (level = ANY (ARRAY['WARNING'::text, 'ERROR'::text]));


--
-- Name: worker_logs_pass_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX worker_logs_pass_ts_idx ON public.worker_logs USING btree (pass, ts DESC) WHERE (pass IS NOT NULL);


--
-- Name: cache_state cache_state_provider_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cache_state
    ADD CONSTRAINT cache_state_provider_fkey FOREIGN KEY (provider) REFERENCES public.providers(slug) ON UPDATE CASCADE;


--
-- Name: cache_state cache_state_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cache_state
    ADD CONSTRAINT cache_state_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: chunk_embeddings chunk_embeddings_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_embeddings
    ADD CONSTRAINT chunk_embeddings_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.chunks(chunk_id) ON DELETE CASCADE;


--
-- Name: chunk_embeddings chunk_embeddings_embedder_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_embeddings
    ADD CONSTRAINT chunk_embeddings_embedder_fkey FOREIGN KEY (embedder) REFERENCES public.embedders(name) ON UPDATE CASCADE;


--
-- Name: chunk_summaries chunk_summaries_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_summaries
    ADD CONSTRAINT chunk_summaries_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.chunks(chunk_id) ON DELETE CASCADE;


--
-- Name: chunk_summaries chunk_summaries_summarizer_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_summaries
    ADD CONSTRAINT chunk_summaries_summarizer_fkey FOREIGN KEY (summarizer) REFERENCES public.summarizers(name) ON UPDATE CASCADE;


--
-- Name: chunk_tags chunk_tags_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_tags
    ADD CONSTRAINT chunk_tags_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.chunks(chunk_id) ON DELETE CASCADE;


--
-- Name: chunk_tags chunk_tags_set_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_tags
    ADD CONSTRAINT chunk_tags_set_by_fkey FOREIGN KEY (set_by) REFERENCES public.actors(slug) ON UPDATE CASCADE;


--
-- Name: chunk_tags chunk_tags_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_tags
    ADD CONSTRAINT chunk_tags_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tags(tag_id) ON DELETE CASCADE;


--
-- Name: chunks chunks_chunk_kind_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_chunk_kind_fkey FOREIGN KEY (chunk_kind) REFERENCES public.chunk_kinds(slug) ON UPDATE CASCADE;


--
-- Name: chunks chunks_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: chunks chunks_set_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_set_by_fkey FOREIGN KEY (set_by) REFERENCES public.actors(slug) ON UPDATE CASCADE;


--
-- Name: cluster_assignments cluster_assignments_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cluster_assignments
    ADD CONSTRAINT cluster_assignments_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.cluster_runs(run_id) ON DELETE CASCADE;


--
-- Name: cluster_cells cluster_cells_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cluster_cells
    ADD CONSTRAINT cluster_cells_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.cluster_runs(run_id) ON DELETE CASCADE;


--
-- Name: dream_transcripts dream_transcripts_attempt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dream_transcripts
    ADD CONSTRAINT dream_transcripts_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES public.dream_log(attempt_id);


--
-- Name: links links_dst_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.links
    ADD CONSTRAINT links_dst_chunk_id_fkey FOREIGN KEY (dst_chunk_id) REFERENCES public.chunks(chunk_id) ON DELETE CASCADE;


--
-- Name: links links_dst_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.links
    ADD CONSTRAINT links_dst_ref_id_fkey FOREIGN KEY (dst_ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: links links_relation_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.links
    ADD CONSTRAINT links_relation_fkey FOREIGN KEY (relation) REFERENCES public.relations(slug) ON UPDATE CASCADE;


--
-- Name: links links_set_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.links
    ADD CONSTRAINT links_set_by_fkey FOREIGN KEY (set_by) REFERENCES public.actors(slug) ON UPDATE CASCADE;


--
-- Name: links links_src_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.links
    ADD CONSTRAINT links_src_chunk_id_fkey FOREIGN KEY (src_chunk_id) REFERENCES public.chunks(chunk_id) ON DELETE CASCADE;


--
-- Name: links links_src_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.links
    ADD CONSTRAINT links_src_ref_id_fkey FOREIGN KEY (src_ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: ref_artifacts ref_artifacts_artifact_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_artifacts
    ADD CONSTRAINT ref_artifacts_artifact_fkey FOREIGN KEY (artifact) REFERENCES public.artifact_kinds(slug) ON UPDATE CASCADE;


--
-- Name: ref_artifacts ref_artifacts_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_artifacts
    ADD CONSTRAINT ref_artifacts_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: ref_events ref_events_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_events
    ADD CONSTRAINT ref_events_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: ref_identifiers ref_identifiers_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_identifiers
    ADD CONSTRAINT ref_identifiers_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: ref_tags ref_tags_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_tags
    ADD CONSTRAINT ref_tags_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: ref_tags ref_tags_set_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_tags
    ADD CONSTRAINT ref_tags_set_by_fkey FOREIGN KEY (set_by) REFERENCES public.actors(slug) ON UPDATE CASCADE;


--
-- Name: ref_tags ref_tags_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_tags
    ADD CONSTRAINT ref_tags_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tags(tag_id) ON DELETE CASCADE;


--
-- Name: refs refs_kind_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refs
    ADD CONSTRAINT refs_kind_fkey FOREIGN KEY (kind) REFERENCES public.kinds(slug) ON UPDATE CASCADE;


--
-- Name: refs refs_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refs
    ADD CONSTRAINT refs_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.refs(ref_id) ON DELETE SET NULL;


--
-- Name: refs refs_pdf_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refs
    ADD CONSTRAINT refs_pdf_sha256_fkey FOREIGN KEY (pdf_sha256) REFERENCES public.pdfs(pdf_sha256) ON DELETE SET NULL;


--
-- Name: refs refs_provider_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refs
    ADD CONSTRAINT refs_provider_fkey FOREIGN KEY (provider) REFERENCES public.providers(slug) ON UPDATE CASCADE;


--
-- Name: refs refs_set_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refs
    ADD CONSTRAINT refs_set_by_fkey FOREIGN KEY (set_by) REFERENCES public.actors(slug) ON UPDATE CASCADE;


--
-- PostgreSQL database dump complete
--

--
-- PostgreSQL database dump
--


-- Dumped from database version 17.9 (Homebrew)
-- Dumped by pg_dump version 17.9 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Data for Name: actors; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.actors (slug, description, created_at) FROM stdin;
agent	LLM-mediated tool call	2026-05-21 21:06:05.179981+01
user	Direct human invocation (CLI, ops)	2026-05-21 21:06:05.179981+01
system	Server-side automation: sweeps, derived state, defaults	2026-05-21 21:06:05.179981+01
chase	Citation-chase worker — automated agent that traces findings to their primary sources and flags misattributions along the chain. See docs/design/finding-chase.md.	2026-05-30 22:33:14.261241+01
\.


--
-- Data for Name: artifact_kinds; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.artifact_kinds (slug, target, storage, output_table, description, deprecated_at, created_at) FROM stdin;
embed:bge-m3	chunk	typed	chunk_embeddings	BGE-M3 1024-dim dense vector	\N	2026-05-30 22:33:14.261241+01
summarize:rake-lemma	chunk	typed	chunk_summaries	RAKE keyword summary (scispacy-lemmatised)	\N	2026-05-30 22:33:14.261241+01
chase_citation	ref	untyped	ref_artifacts	Citation-chase pass result (one hop or terminal)	\N	2026-05-30 22:33:14.261241+01
resolve_citation:s2	ref	untyped	ref_artifacts	Semantic Scholar metadata enrichment for stub refs	\N	2026-05-30 22:33:14.261241+01
keybert:chunks	chunk	typed	chunks	KeyBERT phrases per chunk; abbrev-aware via refs.meta[abbrevs]	\N	2026-06-05 07:56:11.586964+01
embed:tags	tag	typed	tag_embeddings	bge-m3 embeddings of every tag in use, for semantic discovery	\N	2026-06-05 17:35:39.082596+01
\.


--
-- Data for Name: chunk_kinds; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.chunk_kinds (slug, is_card, description, deprecated_at, created_at) FROM stdin;
card_combined	t	Title + authors + abstract + keywords + cite_key	\N	2026-05-21 21:06:05.179981+01
card_title	t	Title only	\N	2026-05-21 21:06:05.179981+01
card_authors	t	Normalised author list	\N	2026-05-21 21:06:05.179981+01
card_abstract	t	Abstract only	\N	2026-05-21 21:06:05.179981+01
card_meta	t	DOI / journal / year / venue	\N	2026-05-21 21:06:05.179981+01
card_keywords	t	RAKE keywords (scispacy-lemmatised, top-50)	\N	2026-05-21 21:06:05.179981+01
paragraph	f	Body paragraph	\N	2026-05-21 21:06:05.179981+01
figure	f	Figure caption + reference	\N	2026-05-21 21:06:05.179981+01
equation	f	Inline or display equation	\N	2026-05-21 21:06:05.179981+01
caption	f	Table / figure caption	\N	2026-05-21 21:06:05.179981+01
heading	f	Section heading (rarely standalone)	\N	2026-05-21 21:06:05.179981+01
references	f	Bibliography section (excluded from default embedding)	\N	2026-05-21 21:06:05.179981+01
code_symbol	f	Function / class / module body	\N	2026-05-21 21:06:05.179981+01
memory_body	f	Memory body text	\N	2026-05-21 21:06:05.179981+01
gripe_body	f	Gripe body text	\N	2026-05-21 21:06:05.179981+01
todo_body	f	Todo body text	\N	2026-05-21 21:06:05.179981+01
conv_message	f	Single message in a conversation	\N	2026-05-21 21:06:05.179981+01
qa_pair	f	Question + answer pair	\N	2026-05-21 21:06:05.179981+01
skill_overview	f	Skill overview section	\N	2026-05-21 21:06:05.179981+01
skill_input	f	Skill input description	\N	2026-05-21 21:06:05.179981+01
skill_output	f	Skill output description	\N	2026-05-21 21:06:05.179981+01
skill_example	f	Skill example	\N	2026-05-21 21:06:05.179981+01
tool_overview	f	Tool overview section	\N	2026-05-21 21:06:05.179981+01
tool_input_schema	f	Tool input schema	\N	2026-05-21 21:06:05.179981+01
tool_output_schema	f	Tool output schema	\N	2026-05-21 21:06:05.179981+01
tool_example	f	Tool example	\N	2026-05-21 21:06:05.179981+01
web_paragraph	f	Paragraph from a cached web result	\N	2026-05-21 21:06:05.179981+01
web_section	f	Section from a cached web result	\N	2026-05-21 21:06:05.179981+01
web_citation	f	Citation from a cached web result	\N	2026-05-21 21:06:05.179981+01
youtube_segment	f	YouTube transcript segment	\N	2026-05-21 21:06:05.179981+01
wolfram_query	f	Wolfram query text	\N	2026-05-21 21:06:05.179981+01
wolfram_response	f	Wolfram response text	\N	2026-05-21 21:06:05.179981+01
decision_section	f	Section of a decision log entry	\N	2026-05-21 21:06:05.179981+01
design_section	f	Section of a design document	\N	2026-05-21 21:06:05.179981+01
patent_claim	f	Individual patent claim	\N	2026-05-21 21:06:05.179981+01
patent_section	f	Patent section (description / drawings)	\N	2026-05-21 21:06:05.179981+01
project_goal	f	Project goal entry	\N	2026-05-21 21:06:05.179981+01
project_constraint	f	Project constraint entry	\N	2026-05-21 21:06:05.179981+01
project_decision_log	f	Project decision-log entry	\N	2026-05-21 21:06:05.179981+01
project_status	f	Project status entry	\N	2026-05-21 21:06:05.179981+01
project_open_question	f	Project open question	\N	2026-05-21 21:06:05.179981+01
project_milestone	f	Project milestone	\N	2026-05-21 21:06:05.179981+01
meeting_segment	f	Meeting transcript segment	\N	2026-05-21 21:06:05.179981+01
action_item	f	Action item from a meeting	\N	2026-05-21 21:06:05.179981+01
meeting_decision	f	Decision recorded in a meeting	\N	2026-05-21 21:06:05.179981+01
email_message	f	Email message body	\N	2026-05-21 21:06:05.179981+01
email_attachment_ref	f	Reference to an email attachment	\N	2026-05-21 21:06:05.179981+01
readme_section	f	README section	\N	2026-05-21 21:06:05.179981+01
commit_message	f	Commit message	\N	2026-05-21 21:06:05.179981+01
issue_comment	f	Comment on an issue	\N	2026-05-21 21:06:05.179981+01
issue_label_change	f	Label change on an issue	\N	2026-05-21 21:06:05.179981+01
issue_milestone	f	Milestone change on an issue	\N	2026-05-21 21:06:05.179981+01
research_report_summary	f	Research-report summary section	\N	2026-05-21 21:06:05.179981+01
research_report_citation	f	Research-report citation entry	\N	2026-05-21 21:06:05.179981+01
finding_body	f	Finding claim text (the measured value plus its bare conditions)	\N	2026-05-30 22:33:14.261241+01
finding_context	f	Finding setup envelope (instrument, electrode, ambient, technique, geometry)	\N	2026-05-30 22:33:14.261241+01
table	f	Markdown table emitted by Marker (skip RAKE).	\N	2026-06-04 20:55:50.15863+01
gripe_comment	f	Gripe comment / append-only timeline entry	\N	2026-06-19 21:13:14.733998+01
job_event	f	Job worker telemetry (forensics, not search)	\N	2026-06-19 21:13:14.733998+01
job_summary	f	Job completion summary (human-readable, searchable)	\N	2026-06-19 21:13:14.733998+01
pres_slide	f	Single slide of a deck (one chunk per slide). Distinct from ``paragraph`` so renderers can show slide numbers and so cross-kind search hits can be labelled as slides.	\N	2026-06-19 21:13:14.782013+01
cron_payload	f	Cron entry body — the natural-language payload that becomes the synthetic prompt to Asa when the cron fires. Searchable; embed + chunk_keywords workers index it normally.	\N	2026-06-19 21:13:14.811139+01
message_body	f	Outbound message body. The text that gets posted. Searchable so past sends can be retrieved with search(kind='message', q='...').	\N	2026-06-19 21:13:14.811139+01
flashcard_claim	f	Flashcard claim side	\N	2026-05-21 21:06:05.179981+01
flashcard_evidence	f	Flashcard evidence side	\N	2026-05-21 21:06:05.179981+01
job_result	f	Per-tick audit chunk written by the planner-coroutine when a plan_tick job finalises (verdict + summary + files). Read by the parent todo's next tick for context.	\N	2026-06-19 21:13:14.948244+01
tag_overflow	f	Long tag-value redirect chunk: when a put attempts to land a tag value longer than 80 chars in a redirectable namespace (ask-user / halt), the full value lands here and the tag becomes ``<ns>:see-chunk-<pos>``.	\N	2026-06-19 21:13:14.948244+01
\.


--
-- Data for Name: embedders; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.embedders (name, dim, is_default, description, deprecated_at, created_at) FROM stdin;
bge-m3	1024	t	BAAI/bge-m3, dense; 1024-dim; multilingual	\N	2026-05-21 21:06:05.179981+01
\.


--
-- Data for Name: kinds; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.kinds (slug, is_numeric, title, description, deprecated_at, created_at) FROM stdin;
paper	f	Paper	Research paper, addressed by cite_key	\N	2026-05-21 21:06:05.179981+01
book	f	Book	Book or monograph	\N	2026-05-21 21:06:05.179981+01
patent	f	Patent	Patent document	\N	2026-05-21 21:06:05.179981+01
research_report	f	Research report	Research / industry report	\N	2026-05-21 21:06:05.179981+01
oracle	f	Oracle	Oracle / authority node	\N	2026-05-21 21:06:05.179981+01
skill	f	Skill	Agent skill document	\N	2026-05-21 21:06:05.179981+01
tool	f	Tool	Tool spec or interface description	\N	2026-05-21 21:06:05.179981+01
code	f	Code symbol	Function, class, module, or repo symbol	\N	2026-05-21 21:06:05.179981+01
decision	f	Decision	ADR-style decision log entry	\N	2026-05-21 21:06:05.179981+01
design	f	Design	Design document / plan	\N	2026-05-21 21:06:05.179981+01
project	f	Project	Project descriptor (goals, status, …)	\N	2026-05-21 21:06:05.179981+01
conv	f	Conversation	Conversation transcript	\N	2026-05-21 21:06:05.179981+01
meeting	f	Meeting	Meeting notes / transcript	\N	2026-05-21 21:06:05.179981+01
email	f	Email	Email message or thread	\N	2026-05-21 21:06:05.179981+01
repo	f	Repo	Source-code repository	\N	2026-05-21 21:06:05.179981+01
issue	f	Issue	Issue tracker item	\N	2026-05-21 21:06:05.179981+01
todo	t	Todo	Task / action item	\N	2026-05-21 21:06:05.179981+01
memory	t	Memory	Note, decision, idea, claim	\N	2026-05-21 21:06:05.179981+01
gripe	t	Gripe	Informal log entry	\N	2026-05-21 21:06:05.179981+01
web	f	Web query	Cached web / research / think query	\N	2026-05-21 21:06:05.179981+01
youtube	f	YouTube	Cached YouTube transcript	\N	2026-05-21 21:06:05.179981+01
math	f	Math result	Cached Wolfram math result	\N	2026-05-21 21:06:05.179981+01
finding	t	Finding	A retrievable empirical claim with explicit setup context and a provenance chain back to its primary source. Synthesised by the citation-chase worker; never externally citable (see docs/design/finding-chase.md).	\N	2026-05-30 22:33:14.261241+01
citation	t	Citation	Verified claim → source pointer. Written by the citation-fill workflow after the verifier confirms the source quote supports the claim.	\N	2026-05-31 15:47:51.530091+01
markdown	f	Markdown file	Read / write .md / .markdown files under a configured root. Slug derived from path; lazy re-ingest on stale mtime; block slugs are content-stable. See src/precis/handlers/markdown.py.	\N	2026-06-04 20:55:50.290874+01
plaintext	f	Plaintext file	Read / write .txt / .org / .rst files under a configured root. The shared file-kind base; markdown and tex are subclasses. See src/precis/handlers/plaintext.py.	\N	2026-06-04 20:55:50.290874+01
tex	f	LaTeX file	Read / write .tex files under a configured root. Inherits the plaintext file-kind machinery; adds tex-aware block parsing + input-resolution. See src/precis/handlers/tex.py.	\N	2026-06-04 20:55:50.290874+01
websearch	f	Web search	Cached perplexity-style web search response. Slug derived from the canonical query + model + freshness window. See src/precis/handlers/perplexity.py.	\N	2026-06-04 21:01:59.625687+01
job	t	Job	Offline run of a task — fix this gripe, run a simulation, benchmark a commit. Addressable by numeric id; status via STATUS: tags; comment timeline via job_event + job_summary chunks.	\N	2026-06-19 21:13:14.733998+01
pres	f	Presentation	Slide deck, unpublished writeup, or other internal document we want indexed but kept separate from the academic paper library. Slug-addressed; one block per slide (or per paragraph for writeups). Subtype carried as ``subtype:slides|writeup|notes|...`` open tag; ``venue`` and ``date`` live in meta. See ``precis-pres-help``.	\N	2026-06-19 21:13:14.782013+01
cron	t	Cron	Scheduled wakeup. The cron-tick CLI scans due entries every 60s, fires pg_notify('precis.cron'), advances next_fire_at per recurrence + catch_up policy. Numeric-id; body lives as a ``cron_payload`` chunk. State in meta.next_fire_at, meta.recurring, meta.catch_up, meta.status. See ``precis-cron-help``.	\N	2026-06-19 21:13:14.811139+01
message	t	Message	Proactive outbound. put(kind='message', target='discord/G/C/T', text='...') stores the ref AND fires pg_notify('precis.messages'). Delivery layer (asa_bot) LISTENs and posts. Numeric-id; one ref per send. Body as ``message_body`` chunk. State in meta.status: 'queued' → 'sent'/'failed'. See ``precis-message-help``.	\N	2026-06-19 21:13:14.811139+01
flashcard	t	Flashcard	Spaced-repetition flashcard	\N	2026-05-21 21:06:05.179981+01
perplexity-reasoning	f	Think	Cached perplexity ``think`` (chain-of-thought) response. Slug derived from the question + model + freshness window. See src/precis/handlers/perplexity.py.	\N	2026-06-04 21:01:59.625687+01
perplexity-research	f	Research report	Cached perplexity ``research`` (deep-research) response. Slug derived from the prompt + model + freshness window. See src/precis/handlers/perplexity.py.	\N	2026-06-04 21:01:59.625687+01
wikipedia	f	Wikipedia (on-demand article fetch)	Resolve a query to the best-matching Wikipedia article via the MediaWiki search API, then fetch and cache its plain-text extract. Slug-addressed by query; cached 7 days; block-split + embedded so search(kind='wikipedia', q=...) lands hits inside fetched articles. On-demand — no bulk dump, always current. See ``precis-wikipedia-help``.	\N	2026-06-19 21:13:15.032906+01
\.


--
-- Data for Name: providers; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.providers (slug, description, deprecated_at, created_at) FROM stdin;
arxiv	arXiv preprint server	\N	2026-05-21 21:06:05.179981+01
crossref	Crossref DOI metadata	\N	2026-05-21 21:06:05.179981+01
s2	Semantic Scholar	\N	2026-05-21 21:06:05.179981+01
pubmed	PubMed / NCBI	\N	2026-05-21 21:06:05.179981+01
openalex	OpenAlex	\N	2026-05-21 21:06:05.179981+01
unpaywall	Unpaywall OA index	\N	2026-05-21 21:06:05.179981+01
perplexity	Perplexity (web / research / think)	\N	2026-05-21 21:06:05.179981+01
wolfram	Wolfram Alpha math	\N	2026-05-21 21:06:05.179981+01
youtube	YouTube transcript	\N	2026-05-21 21:06:05.179981+01
manual	Manually uploaded	\N	2026-05-21 21:06:05.179981+01
local	Local computation / no external source	\N	2026-05-21 21:06:05.179981+01
retraction_watch	Retraction Watch dataset (CC-BY via Crossref)	\N	2026-05-30 17:07:11.520836+01
web	Direct web fetch / trafilatura extraction	\N	2026-05-31 19:20:12.906601+01
epo_ops	European Patent Office Open Patent Services REST API	\N	2026-06-04 21:02:44.133862+01
wikipedia	Wikipedia / MediaWiki API (search + plain-text extracts)	\N	2026-06-19 21:13:15.032906+01
\.


--
-- Data for Name: relations; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.relations (slug, is_symmetric, inverse_slug, description, deprecated_at, created_at) FROM stdin;
related-to	t	\N	Symmetric association	\N	2026-05-21 21:06:05.179981+01
blocks	f	blocked-by	Source blocks target	\N	2026-05-21 21:06:05.179981+01
blocked-by	f	blocks	Source is blocked by target	\N	2026-05-21 21:06:05.179981+01
contradicts	f	contradicted-by	Source contradicts target	\N	2026-05-21 21:06:05.179981+01
contradicted-by	f	contradicts	Source is contradicted by target	\N	2026-05-21 21:06:05.179981+01
cites	f	cited-by	Source cites target	\N	2026-05-21 21:06:05.179981+01
cited-by	f	cites	Source is cited by target	\N	2026-05-21 21:06:05.179981+01
supersedes	f	superseded-by	Source supersedes target	\N	2026-05-21 21:06:05.179981+01
superseded-by	f	supersedes	Source is superseded by target	\N	2026-05-21 21:06:05.179981+01
retracted-by	f	retracts	Source is retracted by target (retraction notice)	\N	2026-05-30 17:07:11.520836+01
retracts	f	retracted-by	Source retracts target	\N	2026-05-30 17:07:11.520836+01
corrected-by	f	corrects	Source is corrected by target (corrigendum/erratum/addendum)	\N	2026-05-30 17:07:11.520836+01
corrects	f	corrected-by	Source corrects target	\N	2026-05-30 17:07:11.520836+01
concern-raised-by	f	raises-concern-about	Source has an Expression of Concern attached	\N	2026-05-30 17:07:11.520836+01
raises-concern-about	f	concern-raised-by	Source raises concern about target	\N	2026-05-30 17:07:11.520836+01
misattributes	f	misattributed-by	Source chunk misrepresents what the target chunk actually says	\N	2026-05-30 22:33:14.261241+01
misattributed-by	f	misattributes	Source chunk is misrepresented by the linked source chunk	\N	2026-05-30 22:33:14.261241+01
derived-from	f	derived-into	Source is derived from target (cause/origin)	\N	2026-05-31 19:20:12.906601+01
derived-into	f	derived-from	Source is the origin from which target derives	\N	2026-05-31 19:20:12.906601+01
supports	f	supported-by	Source provides evidence for target	\N	2026-05-31 19:20:12.906601+01
supported-by	f	supports	Source is supported by target	\N	2026-05-31 19:20:12.906601+01
generalises	f	specialises	Source is a generalisation of target	\N	2026-05-31 19:20:12.906601+01
specialises	f	generalises	Source is a specialisation of target	\N	2026-05-31 19:20:12.906601+01
see-also	f	\N	One-way "for context" pointer (no inverse)	\N	2026-05-31 19:20:12.906601+01
fixes	f	fixed-by	Source ref offers a fix for the target ref (e.g. a fix_gripe job → its gripe)	\N	2026-06-19 21:13:14.749609+01
fixed-by	f	fixes	Source ref is being fixed by the target ref	\N	2026-06-19 21:13:14.749609+01
\.


--
-- Data for Name: summarizers; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.summarizers (name, prompt_template, config, is_default, description, deprecated_at, created_at) FROM stdin;
rake-lemma	\N	{"model": "en_core_sci_sm", "lemmatizer": "scispacy", "max_keywords": 50, "max_phrase_words": 4, "min_phrase_words": 1}	t	RAKE phrase extraction + scispacy lemmatisation	\N	2026-05-21 21:06:05.179981+01
llm-v1	\N	{"alias": "summarizer", "model": "qwen3-next-80b-a3b", "format": "brief;detail", "version": "1", "endpoint": "litellm"}	f	LLM brief+detail chunk summary (Qwen3-Next-80B-A3B via the litellm `summarizer` alias)	\N	2026-06-19 21:13:15.019735+01
\.


--
-- PostgreSQL database dump complete
--

--
-- Migration ledger (synthesised from the migration files, not
-- pg_dump'd) so loading the baseline self-stamps every baked-in
-- version as applied. applied_at is a fixed sentinel.
--
COPY public._migrations (version, applied_at, checksum, plugin) FROM stdin;
0001_initial	1970-01-01 00:00:00+00	7c14c00bb04cb42c9fa38d487d485470971f26459f67daacc556fc39e2568eec	precis
0002_chunk_keywords	1970-01-01 00:00:00+00	99004db354e96a9f1ba653b3b6112e2af9cea982aae746c0eb92e27642f93f7a	precis
0003_drop_legacy_segments	1970-01-01 00:00:00+00	96b03d802afb5aa600ed4a41f90e95f565745618b49fa6ee3d46e6355d6d6447	precis
0004_drop_quest_kind	1970-01-01 00:00:00+00	2deb1c08d136d2ca54c26462e56b19393c101e93826cec38487ab20cf1b979f7	precis
0005_gripe_first_class_and_jobs	1970-01-01 00:00:00+00	de653a1153b8d5be33d446ac54d65ba5552a45fb0572a46f7a651e6d25df1881	precis
0006_fix_gripe_relation	1970-01-01 00:00:00+00	e6c5a6c759987af89e62d6b7f5f604dec84dabf182842a1bf27bfdda5428ec7a	precis
0007_dreaming	1970-01-01 00:00:00+00	3c4ffb26d7e22c2e447e3053d2bc35f17ce9aabea572f645bd89544cc3112906	precis
0008_pres_kind	1970-01-01 00:00:00+00	c67867941be9a4e4fdea735dcce50e364d6ddbcebab1f9d5a344a5268091ab25	precis
0009_ref_events	1970-01-01 00:00:00+00	a62af46063d43095b4f541bdb6e089bd7adf37f499d8d173d1b3ec98ee215d3a	precis
0010_cron_message_tag_ttl	1970-01-01 00:00:00+00	f4b17350bbce31f091186f97284f3dc31132558ae6d40f734aaeac4dc0629ba0	precis
0011_ref_level_decay	1970-01-01 00:00:00+00	07279505306a031763597ced5a8473fcfb6a7d8b66665d9bd46c22cbe42e23e6	precis
0012_epo_ops_provider	1970-01-01 00:00:00+00	aeff304c807b19ab33df5a51b07622e8ff9e39c2a5b5ab0378978d80f5e11502	precis
0013_todo_tree	1970-01-01 00:00:00+00	945061ff08dfbcf7829c8f891f4a162cd160190436c6703efa031542577dc630	precis
0014_refs_prio	1970-01-01 00:00:00+00	b2969b35daf49a662767a7d90265f3b0de0b8070f13a0b15b86d203d4a9600db	precis
0015_worker_logs	1970-01-01 00:00:00+00	238fe85c1c05fbb7c43341b9e0e4de8c7ec61a45ac17a47662e65f2171da3f5d	precis
0016_restore_job_kind	1970-01-01 00:00:00+00	cf24b04a6c214642c85abdd710edf33b8e9da6f8d9c914857518ee1bc8f04e5b	precis
0017_host_heartbeat	1970-01-01 00:00:00+00	2f5f715981a3d8e7bde440a969466ec60af3bc4ba450fbe134cce7dfbd05f2b0	precis
0018_kind_renames_fc_think_research	1970-01-01 00:00:00+00	4b1d1a5f706f0b1a26249a0bd30a009d082853fb2c237424bc60da8f83abee34	precis
0019_chunk_kind_job_result	1970-01-01 00:00:00+00	31a270878b058b4e7261a5a8115a4653fb07f49164dda67e36a9e232a09dfc19	precis
0020_claude_quota_snapshot	1970-01-01 00:00:00+00	d9298887aa685febf0b692cac612940390b882ebcc3776cad158fb023710d545	precis
0021_register_renamed_perplexity_kinds	1970-01-01 00:00:00+00	a4c69e78785fcbcd4a16d8db0c61dcfe6de06b89e16ba87ca943a9e5bdf0e769	precis
0022_kind_provider	1970-01-01 00:00:00+00	a79d0b61c8dda6d034a0453d5798658e28a35bdaa02c2ec3b0e67dc5ba945ef0	precis
0023_migrations_plugin	1970-01-01 00:00:00+00	c37fdf28d0fec87eb969b823bf946892dc237a4a56423819b24f64d52ae0e116	precis
0024_watching	1970-01-01 00:00:00+00	fd3f962ba3ae5a52e957dae3789f2c047a28b5e6d4770c6e5dfad0e446f1427e	precis
0025_register_llm_summarizer	1970-01-01 00:00:00+00	8009713aa52e841817eafc5893c9a49680ede4fe8ecdfbc37222c4df827b32d7	precis
0026_wikipedia_kind	1970-01-01 00:00:00+00	c1758fef24cc3e7f62a1948ce0b5693b66bf087ec22b65ac160c636780ed5299	precis
0027_clusterize	1970-01-01 00:00:00+00	1375dbb59b820bc51a4c8a74b3d3d79f1ac60fe8bcdb0f707d68c77bc84dd0df	precis
0028_normalize_owner_identity_tag	1970-01-01 00:00:00+00	20ff02b321974fa468f2999cdbb999ec8e4eb2a6e098b178bd15c6ebd71c7e90	precis
\.
