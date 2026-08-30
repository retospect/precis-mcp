-- migrations/baseline/schema.sql — generated baseline snapshot.
--
-- DO NOT EDIT BY HAND. Regenerate with `precis db dump-schema`
-- (or `scripts/bump`, which does it at every version bump).
--
-- Baked-in migration head: 0149_refs_retired_at_rename
--
-- This is the migration chain compiled to one file: a fresh
-- `precis migrate` loads this instead of replaying every numbered
-- migration, then applies any migrations added since this snapshot
-- as a normal tail. The numbered migrations stay sealed in the tree
-- as the upgrade path for existing databases. This is NOT
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


-- Dumped from database version 17.10 (Debian 17.10-1.pgdg12+1)
-- Dumped by pg_dump version 18.6

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
-- Name: vault; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA vault;


--
-- Name: bump_salience(bigint[]); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.bump_salience(ids bigint[]) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    UPDATE chunks SET last_seen = now(), accesses = accesses + 1
    WHERE chunk_id = ANY(ids);
$$;


--
-- Name: FUNCTION bump_salience(ids bigint[]); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.bump_salience(ids bigint[]) IS 'Advance last_seen=now() and accesses+1 on a page of chunk ids — the metadata-only access-accounting write on the read path. SECURITY DEFINER so a read-only connection (agent_ro / precis-ro server / write:none envelope) can still heat what it reads; see store/_blocks_ops.py::bump_salience and migration 0079 for the pattern.';


--
-- Name: chunks_forbid_body_text_update(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.chunks_forbid_body_text_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION
        'chunks.text is append-only for body rows '
        '(chunk_id=%, ref_id=%, ord=%, kind=%): an in-place UPDATE orphans '
        'chunk_embeddings/chunk_summaries/keywords. DELETE the row and INSERT '
        'a fresh one so the derived cascade re-runs (AGENTS.md '
        '"Don''t mutate body chunks").',
        OLD.chunk_id, OLD.ref_id, OLD.ord, OLD.chunk_kind
        USING ERRCODE = 'raise_exception';
END;
$$;


--
-- Name: file_gripe_readonly(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.file_gripe_readonly(p_text text) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    v_ref_id bigint;
    v_tag_id bigint;
BEGIN
    IF p_text IS NULL OR length(btrim(p_text)) = 0 THEN
        RAISE EXCEPTION 'file_gripe_readonly: text must not be empty';
    END IF;

    -- ``set_by`` on refs/chunks has an FK into ``actors`` (agent/user/system/
    -- chase); ``session_user`` (the connecting DB role name, e.g. "postgres"
    -- or "agent_ro") is never a registered actor, so — mirroring the
    -- pre-existing ``GripeHandler._create`` behavior, which never passed
    -- ``set_by`` to ``insert_ref``/``insert_blocks`` either — leave both
    -- NULL. Only ``ref_tags.set_by`` is stamped, as ``'agent'`` (a real
    -- actor), matching the old code's ``store.add_tag(..., set_by="agent")``
    -- for the default ``STATUS:open`` tag.
    INSERT INTO refs (kind, title, meta)
    VALUES ('gripe', p_text, '{}'::jsonb)
    RETURNING ref_id INTO v_ref_id;

    -- Mirrors GripeHandler._create's body chunk (pos=0, chunk_kind='gripe_body').
    INSERT INTO chunks (ref_id, ord, chunk_kind, text)
    VALUES (v_ref_id, 0, 'gripe_body', p_text);

    -- Mirrors GripeHandler.default_tags_on_create = ("STATUS:open",).
    INSERT INTO tags (namespace, value) VALUES ('STATUS', 'open')
        ON CONFLICT (namespace, value) DO UPDATE SET namespace = EXCLUDED.namespace
        RETURNING tag_id INTO v_tag_id;
    INSERT INTO ref_tags (ref_id, tag_id, set_by) VALUES (v_ref_id, v_tag_id, 'agent');

    RETURN v_ref_id;
END
$$;


--
-- Name: FUNCTION file_gripe_readonly(p_text text); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.file_gripe_readonly(p_text text) IS 'Insert exactly one gripe (ref + gripe_body chunk + STATUS:open tag) and nothing else. SECURITY DEFINER so an agent_ro connection (write:none envelope) can still file a gripe; see envelope.py + handlers/gripe.py.';


--
-- Name: nanopub_append_only(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.nanopub_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION 'nanopub table % is append-only (spec: proof store must '
        'be immutable and complete); corrections are new rows', TG_TABLE_NAME;
END $$;


--
-- Name: ref_identifiers_lowercase_doi(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.ref_identifiers_lowercase_doi() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.id_kind = 'doi' THEN
        NEW.id_value := lower(NEW.id_value);
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: _hint(text); Type: FUNCTION; Schema: vault; Owner: -
--

CREATE FUNCTION vault._hint(v text) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT CASE
        WHEN v IS NULL OR length(v) = 0 THEN '(empty)'
        WHEN length(v) < 12 THEN repeat(chr(8226), 6) || ' (' || length(v) || ')'
        ELSE left(v, least(3, length(v) / 5)) || chr(8230)
             || right(v, least(2, length(v) / 5))
    END;
$$;


--
-- Name: _key(); Type: FUNCTION; Schema: vault; Owner: -
--

CREATE FUNCTION vault._key() RETURNS text
    LANGUAGE plpgsql STABLE
    AS $$
DECLARE k text;
BEGIN
    k := current_setting('app.secret_key', true);
    IF k IS NULL OR k = '' THEN
        RAISE EXCEPTION 'vault: app.secret_key is not set on this server '
            '(ALTER SYSTEM SET app.secret_key = ...; SELECT pg_reload_conf())';
    END IF;
    RETURN k;
END
$$;


--
-- Name: delete_secret(text); Type: FUNCTION; Schema: vault; Owner: -
--

CREATE FUNCTION vault.delete_secret(p_name text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'vault', 'public', 'pg_temp'
    AS $$
BEGIN
    DELETE FROM vault.secrets WHERE name = p_name;
    INSERT INTO vault.events(who, verb, name) VALUES (session_user, 'delete', p_name);
END
$$;


--
-- Name: gc_events(integer); Type: FUNCTION; Schema: vault; Owner: -
--

CREATE FUNCTION vault.gc_events(p_keep_days integer DEFAULT 180) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'vault', 'public', 'pg_temp'
    AS $$
DECLARE n bigint;
BEGIN
    DELETE FROM vault.events
     WHERE at < now() - make_interval(days => p_keep_days);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END
$$;


--
-- Name: list(); Type: FUNCTION; Schema: vault; Owner: -
--

CREATE FUNCTION vault.list() RETURNS TABLE(name text, hint text, updated_at timestamp with time zone)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'vault', 'public', 'pg_temp'
    AS $$
    SELECT s.name, s.hint, s.updated_at FROM vault.secrets s ORDER BY s.name;
$$;


--
-- Name: mask(text); Type: FUNCTION; Schema: vault; Owner: -
--

CREATE FUNCTION vault.mask(p_name text) RETURNS text
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'vault', 'public', 'pg_temp'
    AS $$
    SELECT s.hint FROM vault.secrets s WHERE s.name = p_name;
$$;


--
-- Name: reveal(text); Type: FUNCTION; Schema: vault; Owner: -
--

CREATE FUNCTION vault.reveal(p_name text) RETURNS text
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'vault', 'public', 'pg_temp'
    AS $$
    SELECT vault.reveal(p_name, NULL::text, NULL::text,
                        NULL::integer, NULL::integer, NULL::text);
$$;


--
-- Name: reveal(text, text, text, integer, integer, text); Type: FUNCTION; Schema: vault; Owner: -
--

CREATE FUNCTION vault.reveal(p_name text, p_host text, p_os_user text, p_pid integer, p_ppid integer, p_process text) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'vault', 'public', 'pg_temp'
    AS $$
DECLARE v text;
BEGIN
    SELECT pgp_sym_decrypt(s.ciphertext, vault._key())
        INTO v FROM vault.secrets s WHERE s.name = p_name;
    IF v IS NULL THEN
        RETURN NULL;   -- unknown name; not an error (callers fall back)
    END IF;
    INSERT INTO vault.events(who, verb, name, host, os_user, pid, ppid, process)
    VALUES (session_user, 'reveal', p_name,
            p_host, p_os_user, p_pid, p_ppid, p_process);
    RETURN v;
END
$$;


--
-- Name: set_secret(text, text); Type: FUNCTION; Schema: vault; Owner: -
--

CREATE FUNCTION vault.set_secret(p_name text, p_value text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'vault', 'public', 'pg_temp'
    AS $$
BEGIN
    IF p_value IS NULL OR length(p_value) = 0 THEN
        RAISE EXCEPTION 'vault: refusing to store an empty value for %', p_name;
    END IF;
    INSERT INTO vault.secrets(name, ciphertext, hint)
    VALUES (p_name,
            pgp_sym_encrypt(p_value, vault._key()),
            vault._hint(p_value))
    ON CONFLICT (name) DO UPDATE
        SET ciphertext = EXCLUDED.ciphertext,
            hint       = EXCLUDED.hint,
            updated_at = now();
    INSERT INTO vault.events(who, verb, name) VALUES (session_user, 'set', p_name);
END
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
-- Name: app_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_settings (
    key text NOT NULL,
    value text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text
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
-- Name: cad_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cad_nodes (
    node_id bigint NOT NULL,
    ref_id bigint NOT NULL,
    ord integer NOT NULL,
    name text NOT NULL,
    component text NOT NULL,
    op text NOT NULL,
    config text NOT NULL,
    loc double precision[] DEFAULT '{0,0,0}'::double precision[] NOT NULL,
    rot double precision[] DEFAULT '{0,0,0}'::double precision[] NOT NULL,
    pattern jsonb,
    operands bigint[],
    retired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE cad_nodes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.cad_nodes IS 'CAD design nodes (ADR 0041 Amendment 1): one placed primitive / boolean operator per row, owned by a kind=cad ref. Structured geometry — never embedded; probes fold these on demand.';


--
-- Name: cad_nodes_node_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.cad_nodes ALTER COLUMN node_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cad_nodes_node_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: chunk_blobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk_blobs (
    chunk_id bigint NOT NULL,
    bytes bytea NOT NULL,
    mime text NOT NULL,
    sha256 character(64) NOT NULL,
    size_bytes bigint NOT NULL,
    width integer,
    height integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chunk_citations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk_citations (
    id bigint NOT NULL,
    chunk_id bigint NOT NULL,
    marker integer NOT NULL,
    bib_entry_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chunk_citations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.chunk_citations ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.chunk_citations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: chunk_claims; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk_claims (
    chunk_id bigint NOT NULL,
    artifact text NOT NULL,
    claimed_at timestamp with time zone DEFAULT now() NOT NULL,
    attempts integer DEFAULT 0 NOT NULL
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
    content_sha text,
    CONSTRAINT chunk_embeddings_status_check CHECK ((status = ANY (ARRAY['ok'::text, 'failed'::text])))
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.01', autovacuum_vacuum_cost_delay='0');


--
-- Name: chunk_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk_events (
    event_id bigint NOT NULL,
    chunk_id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    event_kind text NOT NULL,
    content_sha text,
    prev_text text,
    source jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT chunk_events_event_kind_check CHECK ((event_kind = ANY (ARRAY['created'::text, 'edited'::text, 'moved'::text, 'reparented'::text, 'retired'::text, 'restored'::text])))
);


--
-- Name: chunk_events_event_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chunk_events_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chunk_events_event_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chunk_events_event_id_seq OWNED BY public.chunk_events.event_id;


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
-- Name: chunk_review; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk_review (
    chunk_id bigint NOT NULL,
    checker text NOT NULL,
    approved_sha text NOT NULL,
    verdict text NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL
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
    content_sha text,
    CONSTRAINT chunk_summaries_status_check CHECK ((status = ANY (ARRAY['ok'::text, 'failed'::text])))
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.01', autovacuum_vacuum_cost_delay='0');


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
    handle text,
    pos text,
    parent_chunk_id bigint,
    content_sha text,
    retired_at timestamp with time zone,
    CONSTRAINT chunks_check CHECK ((((ord < 0) AND (chunk_kind ~~ 'card_%'::text)) OR ((ord >= 0) AND (chunk_kind !~~ 'card_%'::text)))),
    CONSTRAINT chunks_check1 CHECK (((page_first IS NULL) OR (page_last IS NULL) OR (page_first <= page_last)))
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.01', autovacuum_vacuum_cost_delay='0');


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
-- Name: claim_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.claim_embeddings (
    hub_ref_id bigint NOT NULL,
    embedder text NOT NULL,
    claim_sha text NOT NULL,
    vector public.vector(1024),
    embedded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE claim_embeddings; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.claim_embeddings IS 'Embedding index over taproot claim hubs (findings tagged TAPROOT:claim). Probed per new chunk by the chase_trigger pass to mark affected claims due (TAPROOT_DUE tag). One vector per (hub, embedder); claim_sha gates re-embed on claim edit. See migration 0101/0144 / workers/chase_trigger.py.';


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
-- Name: component_categories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.component_categories (
    category_id text NOT NULL,
    name text NOT NULL,
    status text DEFAULT 'proposed'::text NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT component_categories_status_check CHECK ((status = ANY (ARRAY['core'::text, 'proposed'::text])))
);


--
-- Name: TABLE component_categories; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.component_categories IS 'Growable, flat (no taxonomy tree) component-category registry (component-kind proposal). core = curated starter set; proposed = minted at entity-write time, never silently promoted.';


--
-- Name: component_spec_values; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.component_spec_values (
    id bigint NOT NULL,
    component_ref_id bigint NOT NULL,
    spec_id text NOT NULL,
    value_num double precision,
    value_low double precision,
    value_high double precision,
    value_text text,
    value_bool boolean,
    input_unit text,
    conditions jsonb DEFAULT '{}'::jsonb NOT NULL,
    maturity text DEFAULT 'lab'::text NOT NULL,
    method text,
    source_ref_id bigint,
    source_chunk text,
    source_url text,
    as_of date,
    set_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    notes text,
    CONSTRAINT component_spec_values_maturity_check CHECK ((maturity = ANY (ARRAY['commercial'::text, 'lab'::text, 'speculative'::text])))
);


--
-- Name: TABLE component_spec_values; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.component_spec_values IS 'component-kind proposal: one sourced measurement per row. component_ref_id is handler-enforced to kind=component (refs has no per-kind FK). value_num is stored in the spec''s canonical unit (v1 does no conversion). Per-unit cost is just the universal unit_cost spec, paired with as_of + conditions={qty_break}.';


--
-- Name: component_spec_values_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.component_spec_values ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.component_spec_values_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: component_specs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.component_specs (
    spec_id text NOT NULL,
    name text NOT NULL,
    canonical_unit text,
    dimension text,
    value_type text NOT NULL,
    allowed_values jsonb,
    standard_ref text,
    status text DEFAULT 'proposed'::text NOT NULL,
    higher_is_better boolean,
    description text,
    category_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT component_specs_status_check CHECK ((status = ANY (ARRAY['core'::text, 'proposed'::text]))),
    CONSTRAINT component_specs_value_type_check CHECK ((value_type = ANY (ARRAY['quantity'::text, 'ratio'::text, 'categorical'::text, 'boolean'::text, 'text'::text])))
);


--
-- Name: TABLE component_specs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.component_specs IS 'Typed, growable, category-scoped component-spec registry (component-kind proposal). category_id IS NULL = universal (mass/unit_cost/length_overall); non-NULL = scoped to that category, handler-enforced at write time. core = curated starter set; proposed = minted at write time (must declare a canonical unit + dimension), never silently promoted.';


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
-- Name: email_account; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_account (
    account text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    secret_name text NOT NULL,
    last_uid bigint DEFAULT 0 NOT NULL,
    uidvalidity bigint,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_polled_at timestamp with time zone,
    consecutive_errors integer DEFAULT 0 NOT NULL,
    last_status text
);


--
-- Name: TABLE email_account; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.email_account IS 'Per-account IMAP/SMTP registry for the email kind (secret in vault, not here); config JSONB carries imap/smtp/folders/poll_seconds/auth/scan_policy.';


--
-- Name: email_scan; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_scan (
    account text NOT NULL,
    folder text NOT NULL,
    uidvalidity bigint NOT NULL,
    uid bigint NOT NULL,
    verdict text NOT NULL,
    depth smallint NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    scanned_at timestamp with time zone DEFAULT now() NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone
);


--
-- Name: TABLE email_scan; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.email_scan IS 'Per-message injection-scan verdict for the email kind (no body stored); keyed by (account,folder,uidvalidity,uid). depth 0 = mail_poll regex.';


--
-- Name: COLUMN email_scan.depth; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.email_scan.depth IS 'How many scan passes deep this verdict reached: 0 = mail_poll''s inline regex, 1 = the model rung, 2 = the escalated model (workers/inject_scan.py). Renamed from `tier` (vocab-compaction Stage C) to stop colliding with the LLM router''s unrelated `Tier` capability band.';


--
-- Name: COLUMN email_scan.attempts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.email_scan.attempts IS 'Count of inject_scan model-call attempts against this row (any tier<1 row); bumped at claim time, before the call.';


--
-- Name: COLUMN email_scan.next_attempt_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.email_scan.next_attempt_at IS 'Claim-time cooldown -- pending_email_scans excludes a row while this is in the future; NULL = eligible now.';


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
-- Name: external_rate_limits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_rate_limits (
    provider text NOT NULL,
    capacity integer NOT NULL,
    refill_per_sec numeric NOT NULL,
    tokens numeric NOT NULL,
    last_refill timestamp with time zone DEFAULT now() NOT NULL,
    daily_cap integer,
    day_used integer DEFAULT 0 NOT NULL,
    day_start date DEFAULT CURRENT_DATE NOT NULL
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
)
WITH (autovacuum_vacuum_scale_factor='0', autovacuum_vacuum_threshold='200', autovacuum_vacuum_cost_delay='0');


--
-- Name: host_heartbeat_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.host_heartbeat_log (
    host text NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    temp_c double precision,
    load1 double precision,
    load5 double precision,
    load15 double precision
);


--
-- Name: TABLE host_heartbeat_log; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.host_heartbeat_log IS 'Append-only per-beat sensor history (load + temp). Written by the heartbeat pass alongside the host_heartbeat snapshot UPSERT; pruned to PRECIS_HEARTBEAT_HISTORY_DAYS. Read by precis stats --utilization.';


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
-- Name: llm_blob; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_blob (
    hash text NOT NULL,
    text text NOT NULL,
    bytes integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: llm_call_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_call_log (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    source text,
    tier text,
    transport text,
    model text,
    tools_needed boolean,
    request_hash text,
    response_hash text,
    request_chars integer,
    response_chars integer,
    cost_usd double precision,
    turns_used integer,
    duration_ms integer,
    errored boolean DEFAULT false NOT NULL,
    error text,
    data_parsed boolean,
    ref_id bigint,
    features jsonb,
    placement text,
    input_tokens integer,
    output_tokens integer,
    cache_read_tokens integer,
    cache_creation_tokens integer
);


--
-- Name: COLUMN llm_call_log.placement; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.llm_call_log.placement IS 'local | cloud for the rung that ran (router._placement_of). Local rows carry a PRICED cost_usd, not money spent — the planner dollar caps exclude them. NULL (pre-0112) is treated as cloud.';


--
-- Name: COLUMN llm_call_log.input_tokens; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.llm_call_log.input_tokens IS 'Prompt tokens reported by the provider (LlmResult.input_tokens). NULL when the transport reports none (e.g. claude_p) or predates this column.';


--
-- Name: COLUMN llm_call_log.output_tokens; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.llm_call_log.output_tokens IS 'Completion tokens reported by the provider (LlmResult.output_tokens).';


--
-- Name: COLUMN llm_call_log.cache_read_tokens; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.llm_call_log.cache_read_tokens IS 'Prompt-cache-read tokens (LlmResult.cache_read_tokens) — billed at a discount, so kept separate from input_tokens rather than folded in.';


--
-- Name: COLUMN llm_call_log.cache_creation_tokens; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.llm_call_log.cache_creation_tokens IS 'Prompt-cache-write tokens (LlmResult.cache_creation_tokens).';


--
-- Name: llm_call_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.llm_call_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: llm_call_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.llm_call_log_id_seq OWNED BY public.llm_call_log.id;


--
-- Name: material_properties; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.material_properties (
    prop_id text NOT NULL,
    name text NOT NULL,
    canonical_unit text,
    dimension text,
    value_type text NOT NULL,
    allowed_values jsonb,
    standard_ref text,
    status text DEFAULT 'proposed'::text NOT NULL,
    higher_is_better boolean,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT material_properties_status_check CHECK ((status = ANY (ARRAY['core'::text, 'proposed'::text]))),
    CONSTRAINT material_properties_value_type_check CHECK ((value_type = ANY (ARRAY['quantity'::text, 'ratio'::text, 'categorical'::text, 'boolean'::text, 'text'::text])))
);


--
-- Name: TABLE material_properties; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.material_properties IS 'Typed, growable material-property registry (materials-handbook-kind proposal). core = curated starter set; proposed = minted at write time (must declare a canonical unit + dimension), never silently promoted.';


--
-- Name: material_values; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.material_values (
    id bigint NOT NULL,
    material_ref_id bigint NOT NULL,
    property_id text NOT NULL,
    value_num double precision,
    value_low double precision,
    value_high double precision,
    value_text text,
    value_bool boolean,
    input_unit text,
    conditions jsonb DEFAULT '{}'::jsonb NOT NULL,
    maturity text DEFAULT 'lab'::text NOT NULL,
    method text,
    source_ref_id bigint,
    source_chunk text,
    source_url text,
    as_of date,
    set_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    notes text,
    CONSTRAINT material_values_maturity_check CHECK ((maturity = ANY (ARRAY['commercial'::text, 'lab'::text, 'speculative'::text])))
);


--
-- Name: TABLE material_values; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.material_values IS 'materials-handbook-kind proposal: one sourced measurement per row. material_ref_id is handler-enforced to kind=material (refs has no per-kind FK). value_num is stored in the property''s canonical unit (v1 does no conversion).';


--
-- Name: material_values_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.material_values ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.material_values_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: nanopub_artifacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nanopub_artifacts (
    id bigint NOT NULL,
    publish_id bigint NOT NULL,
    claim_ref_id bigint NOT NULL,
    artifact_type text NOT NULL,
    trig_bytes bytea NOT NULL,
    byte_sha256 text GENERATED ALWAYS AS (encode(sha256(trig_bytes), 'hex'::text)) STORED,
    trusty_uri text NOT NULL,
    aida_uri text NOT NULL,
    claim_sha text NOT NULL,
    signer text NOT NULL,
    key_fingerprint text NOT NULL,
    dois jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE nanopub_artifacts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.nanopub_artifacts IS 'Append-only signed-artifact store: exact TriG bytes + indexed extracts. byte_sha256 is generated from the bytes and doubles as the OTS leaf digest. Superseded artifacts stay forever.';


--
-- Name: nanopub_artifacts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.nanopub_artifacts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: nanopub_artifacts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.nanopub_artifacts_id_seq OWNED BY public.nanopub_artifacts.id;


--
-- Name: nanopub_mirror; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nanopub_mirror (
    artifact_code text NOT NULL,
    trig_bytes bytea NOT NULL,
    byte_sha256 text GENERATED ALWAYS AS (encode(sha256(trig_bytes), 'hex'::text)) STORED,
    source_url text NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    verified boolean DEFAULT false NOT NULL,
    aida_uri text,
    signer text,
    key_fingerprint text,
    dois jsonb,
    assertion_predicates jsonb,
    retracted_by text,
    superseded_by text
);


--
-- Name: TABLE nanopub_mirror; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.nanopub_mirror IS 'Read-only cache of external published nanopubs: exact fetched bytes + trusty-recompute verification + rebuildable index extracts (docs/backlog/nanopub-registry-mirror.md). Not our proof store; no append-only trigger.';


--
-- Name: nanopub_mirror_edges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nanopub_mirror_edges (
    id bigint NOT NULL,
    from_code text NOT NULL,
    to_code text NOT NULL,
    relation text NOT NULL,
    CONSTRAINT nanopub_mirror_edges_relation_check CHECK ((relation = ANY (ARRAY['retracts'::text, 'supersedes'::text, 'refers-to'::text])))
);


--
-- Name: TABLE nanopub_mirror_edges; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.nanopub_mirror_edges IS 'np→np references extracted from mirrored bytes. to_code is not an FK (open-world arrival order; multiple retraction claimants).';


--
-- Name: nanopub_mirror_edges_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.nanopub_mirror_edges_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: nanopub_mirror_edges_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.nanopub_mirror_edges_id_seq OWNED BY public.nanopub_mirror_edges.id;


--
-- Name: nanopub_ots_batches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nanopub_ots_batches (
    id bigint NOT NULL,
    merkle_root text NOT NULL,
    construction text NOT NULL,
    leaf_count integer NOT NULL,
    calendar_url text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT nanopub_ots_batches_leaf_count_check CHECK ((leaf_count > 0))
);


--
-- Name: nanopub_ots_batches_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.nanopub_ots_batches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: nanopub_ots_batches_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.nanopub_ots_batches_id_seq OWNED BY public.nanopub_ots_batches.id;


--
-- Name: nanopub_ots_leaves; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nanopub_ots_leaves (
    id bigint NOT NULL,
    batch_id bigint NOT NULL,
    artifact_id bigint NOT NULL,
    leaf_index integer NOT NULL,
    leaf_hash text NOT NULL,
    path_proof bytea NOT NULL
);


--
-- Name: nanopub_ots_leaves_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.nanopub_ots_leaves_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: nanopub_ots_leaves_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.nanopub_ots_leaves_id_seq OWNED BY public.nanopub_ots_leaves.id;


--
-- Name: nanopub_ots_proofs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nanopub_ots_proofs (
    id bigint NOT NULL,
    batch_id bigint NOT NULL,
    state text NOT NULL,
    ots_proof bytea NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT nanopub_ots_proofs_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'upgraded'::text])))
);


--
-- Name: nanopub_ots_proofs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.nanopub_ots_proofs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: nanopub_ots_proofs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.nanopub_ots_proofs_id_seq OWNED BY public.nanopub_ots_proofs.id;


--
-- Name: nanopub_publish; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nanopub_publish (
    id bigint NOT NULL,
    claim_ref_id bigint NOT NULL,
    artifact_type text DEFAULT 'claim'::text NOT NULL,
    approved_title text,
    claim_sha text,
    aida_uri text,
    grounding jsonb,
    dependency_codes jsonb,
    trusty_uri text,
    artifact_id bigint,
    batch_id bigint,
    state text DEFAULT 'candidate'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    registry_url text,
    CONSTRAINT nanopub_publish_artifact_type_check CHECK ((artifact_type = ANY (ARRAY['claim'::text, 'compound'::text, 'hypothesis'::text]))),
    CONSTRAINT nanopub_publish_state_check CHECK ((state = ANY (ARRAY['candidate'::text, 'reviewed'::text, 'signed'::text, 'anchored'::text, 'published'::text, 'superseded'::text, 'retracted'::text, 'rejected'::text])))
);


--
-- Name: TABLE nanopub_publish; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.nanopub_publish IS 'One live publish row per claim hub: frozen approved string, claim_sha drift gate, AIDA URI, grounding, and the mint/publish state machine. Working copy vs frozen artifact bytes is the duplication the crypto requires (docs/backlog/claim-publication-nanopub-ots.md).';


--
-- Name: nanopub_publish_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.nanopub_publish_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: nanopub_publish_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.nanopub_publish_id_seq OWNED BY public.nanopub_publish.id;


--
-- Name: nanopub_trust_allowlist; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nanopub_trust_allowlist (
    id bigint NOT NULL,
    identity_uri text NOT NULL,
    key_fingerprint text NOT NULL,
    attesting boolean DEFAULT false NOT NULL,
    valid_from timestamp with time zone DEFAULT now() NOT NULL,
    valid_until timestamp with time zone,
    note text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE nanopub_trust_allowlist; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.nanopub_trust_allowlist IS 'Publication-time trust gate: only signatures whose (identity, key fingerprint) pair is listed here are trusted at publish; attesting=TRUE marks the human key ("only human-attested claims are publishable"). Flat, zero transitivity; keys pinned, never bare identities (docs/backlog/claim-publication-nanopub-ots.md).';


--
-- Name: nanopub_trust_allowlist_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.nanopub_trust_allowlist_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: nanopub_trust_allowlist_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.nanopub_trust_allowlist_id_seq OWNED BY public.nanopub_trust_allowlist.id;


--
-- Name: news_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_sources (
    source_id bigint NOT NULL,
    url text NOT NULL,
    title text NOT NULL,
    source_slug text NOT NULL,
    category text,
    default_tags text[] DEFAULT '{}'::text[] NOT NULL,
    max_items integer DEFAULT 50 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    etag text,
    last_modified text,
    last_polled_at timestamp with time zone,
    last_status text,
    consecutive_errors integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE news_sources; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.news_sources IS 'Operator-editable RSS/Atom feed list for the news_poll worker. One row per feed; disable with enabled=false rather than deleting.';


--
-- Name: news_sources_source_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.news_sources ALTER COLUMN source_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.news_sources_source_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: paper_bib_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.paper_bib_entries (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    marker integer NOT NULL,
    raw_text text NOT NULL,
    authors text,
    journal text,
    year integer,
    volume text,
    first_page text,
    doi text,
    s2_id text,
    held_ref_id bigint,
    parse_conf real,
    match_conf real,
    parse_version integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: paper_bib_entries_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.paper_bib_entries ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.paper_bib_entries_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: part_availability; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.part_availability (
    lcsc text NOT NULL,
    stock_now integer,
    stock_prev integer,
    ewma_stock double precision,
    restock_count integer DEFAULT 0 NOT NULL,
    last_restock_at timestamp with time zone,
    trend double precision,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    discontinued boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE part_availability; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.part_availability IS 'Per-part turnover signal (ADR 0042 §5) — diffed from daily dumps; survives the catalog swap; selection ranks on this, not live stock.';


--
-- Name: part_footprints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.part_footprints (
    lcsc text NOT NULL,
    pads jsonb,
    pin_map jsonb,
    courtyard jsonb,
    centroid jsonb,
    kicad_mod text,
    model_3d text,
    source text,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    raw jsonb,
    escape jsonb
);


--
-- Name: TABLE part_footprints; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.part_footprints IS 'easyeda2kicad footprint cache (ADR 0042 §5, Flow B) — lazy per selected part; keyed by C-number; never touched by the catalog swap.';


--
-- Name: COLUMN part_footprints.raw; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.part_footprints.raw IS 'Untouched EasyEDA component JSON (GET easyeda.com/api/products/<C>/components) kept for reparse without re-fetching a third-party host.';


--
-- Name: COLUMN part_footprints.escape; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.part_footprints.escape IS 'Precomputed footprint escape graph (precis.pcb.escape.EscapeGraph, shells/gaps/per_shell_capacity/required_layers) — footprint-intrinsic, cached once per footprint, never recomputed per placement.';


--
-- Name: parts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.parts (
    lcsc text NOT NULL,
    mfr text,
    mfr_part text,
    description text,
    jlcpcb_assemblable boolean DEFAULT false NOT NULL,
    basic boolean DEFAULT false NOT NULL,
    stock integer,
    price jsonb,
    package text,
    height_mm double precision,
    params jsonb,
    datasheet_url text,
    description_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, COALESCE(description, ''::text))) STORED,
    refreshed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE parts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.parts IS 'LCSC/JLCPCB catalog (ADR 0042 §5, Flow A) — bulk from the jlcparts dump via staging + atomic swap. NO inbound FK (the swap drops the table).';


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
-- Name: pcb_boards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_boards (
    board_id bigint NOT NULL,
    ref_id bigint NOT NULL,
    name text DEFAULT 'main'::text NOT NULL,
    stackup jsonb NOT NULL,
    fold_lines jsonb DEFAULT '[]'::jsonb NOT NULL,
    note text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    retired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE pcb_boards; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_boards IS 'A physical board of a design (pcb-guided-place-route Slice 1) — stackup as ordered jsonb (boards are few, stackups read as a unit); fold_lines geometry (empty in v1, flex/rigid-flex hedge). v1: exactly one board per design, name ''main''.';


--
-- Name: pcb_boards_board_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_boards ALTER COLUMN board_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_boards_board_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_components; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_components (
    component_id bigint NOT NULL,
    ref_id bigint NOT NULL,
    label text NOT NULL,
    part_lcsc text,
    footprint text,
    courtyard jsonb,
    centroid jsonb,
    height_mm double precision,
    note text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    retired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE pcb_components; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_components IS 'PCB component TYPE (ADR 0042 §4) — owns pcb_pins; loose-refs a catalog SKU; snapshots footprint/centroid so a design survives catalog churn.';


--
-- Name: pcb_components_component_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_components ALTER COLUMN component_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_components_component_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_copper; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_copper (
    copper_id bigint NOT NULL,
    board_id bigint NOT NULL,
    ctype text NOT NULL,
    layer text NOT NULL,
    net_id bigint NOT NULL,
    route_id bigint,
    geom jsonb NOT NULL,
    generated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pcb_copper_ctype_chk CHECK ((ctype = ANY (ARRAY['track'::text, 'via'::text, 'pour'::text])))
);


--
-- Name: TABLE pcb_copper; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_copper IS 'DERIVED realized copper (pcb-guided-place-route) — regenerated wholesale (DELETE board''s rows + INSERT) per realize run, the same cascade discipline as chunks->embeddings. Never hand-edited, no retired_at — a realize run replaces, it does not soft-delete.';


--
-- Name: pcb_copper_copper_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_copper ALTER COLUMN copper_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_copper_copper_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_drc_findings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_drc_findings (
    finding_id bigint NOT NULL,
    board_id bigint NOT NULL,
    run_id text NOT NULL,
    rule text NOT NULL,
    severity text NOT NULL,
    objects jsonb DEFAULT '[]'::jsonb NOT NULL,
    detail text,
    waived_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pcb_drc_findings_severity_chk CHECK ((severity = ANY (ARRAY['error'::text, 'warn'::text])))
);


--
-- Name: TABLE pcb_drc_findings; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_drc_findings IS 'Durable, linkable DRC results (pcb-guided-place-route) per (board, run_id) — gate evaluators and the LLM read the latest run.';


--
-- Name: pcb_drc_findings_finding_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_drc_findings ALTER COLUMN finding_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_drc_findings_finding_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_features; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_features (
    feature_id bigint NOT NULL,
    ref_id bigint NOT NULL,
    ftype text NOT NULL,
    x double precision,
    y double precision,
    rot double precision DEFAULT 0 NOT NULL,
    layer text,
    fixed text,
    geom jsonb,
    note text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    retired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    board_id bigint NOT NULL
);


--
-- Name: TABLE pcb_features; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_features IS 'Non-electrical placed features (ADR 0042 §4): mounting holes, fiducials, keepouts, the board outline.';


--
-- Name: pcb_features_feature_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_features ALTER COLUMN feature_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_features_feature_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_instances; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_instances (
    instance_id bigint NOT NULL,
    ref_id bigint NOT NULL,
    component_id bigint NOT NULL,
    refdes text NOT NULL,
    x double precision,
    y double precision,
    rot double precision DEFAULT 0 NOT NULL,
    layer text DEFAULT 'top'::text NOT NULL,
    fixed text,
    roles text[] DEFAULT '{}'::text[] NOT NULL,
    note text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    retired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    board_id bigint NOT NULL,
    CONSTRAINT pcb_instances_fixed_chk CHECK (((fixed IS NULL) OR (fixed = ANY (ARRAY['xy'::text, 'rot'::text, 'both'::text])))),
    CONSTRAINT pcb_instances_layer_chk CHECK ((layer = ANY (ARRAY['top'::text, 'bottom'::text])))
);


--
-- Name: TABLE pcb_instances; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_instances IS 'A placement (refdes) of a component (ADR 0042 §4) — centroid x/y, rot (CW from north), layer, fixed, roles, note.';


--
-- Name: pcb_instances_instance_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_instances ALTER COLUMN instance_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_instances_instance_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_measures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_measures (
    measure_id bigint NOT NULL,
    ref_id bigint NOT NULL,
    metric text NOT NULL,
    direction text,
    goal double precision,
    strength text DEFAULT 'gauge'::text NOT NULL,
    weight double precision,
    operands jsonb NOT NULL,
    reason text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    retired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pcb_measures_strength_chk CHECK ((strength = ANY (ARRAY['hard'::text, 'soft'::text, 'gauge'::text])))
);


--
-- Name: TABLE pcb_measures; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_measures IS 'PCB measures (ADR 0042 §8.3) — the measuring tapes; hard/soft/gauge design intent over instances/nets/classes, re-evaluated on change.';


--
-- Name: pcb_measures_measure_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_measures ALTER COLUMN measure_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_measures_measure_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_net_classes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_net_classes (
    class_id bigint NOT NULL,
    ref_id bigint NOT NULL,
    name text NOT NULL,
    rules jsonb DEFAULT '{}'::jsonb NOT NULL,
    note text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    retired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE pcb_net_classes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_net_classes IS 'Per-design net-class rules (pcb-guided-place-route Slice 1) — joined by pcb_nets.net_class = name; a missing row means built-in defaults. The router/DRC read rules only from here, never assume copper.';


--
-- Name: pcb_net_classes_class_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_net_classes ALTER COLUMN class_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_net_classes_class_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_netconns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_netconns (
    netconn_id bigint NOT NULL,
    net_id bigint NOT NULL,
    instance_id bigint NOT NULL,
    pin_id bigint NOT NULL,
    component_id bigint NOT NULL,
    note text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE pcb_netconns; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_netconns IS 'The netlist (ADR 0042 §4): one row per (net, instance, pin). A physical pin is on at most one net. Composite FKs force pin.component = instance.component. note = why this wire. Hard-delete (re-wire = delete+insert).';


--
-- Name: pcb_netconns_netconn_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_netconns ALTER COLUMN netconn_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_netconns_netconn_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_nets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_nets (
    net_id bigint NOT NULL,
    ref_id bigint NOT NULL,
    name text NOT NULL,
    net_class text,
    est_current_a double precision,
    width_mm double precision,
    note text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    retired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    domain text DEFAULT 'electrical'::text NOT NULL,
    CONSTRAINT pcb_nets_domain_chk CHECK ((domain = ANY (ARRAY['electrical'::text, 'fluidic'::text, 'thermal'::text])))
);


--
-- Name: TABLE pcb_nets; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_nets IS 'PCB nets (ADR 0042 §4) — REQUIRED meaningful name (the net''s purpose), class, est current, derived width.';


--
-- Name: COLUMN pcb_nets.domain; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pcb_nets.domain IS 'electrical|fluidic|thermal (pcb-guided-place-route hedge). v1 routes electrical only; the handler rejects fluidic/thermal at put with a clear message — the column is schema-reserved for later co-design.';


--
-- Name: pcb_nets_net_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_nets ALTER COLUMN net_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_nets_net_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_pin_swaps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_pin_swaps (
    swap_id bigint NOT NULL,
    board_id bigint NOT NULL,
    instance_id bigint NOT NULL,
    pin_id bigint NOT NULL,
    component_id bigint NOT NULL,
    net_id bigint NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    retired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE pcb_pin_swaps; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_pin_swaps IS 'DERIVED pin<->net override (pcb-engine-plan "PIN_SWAP is not persisted") — one row per physical pin whose effective net differs from pcb_netconns, gr267526''s provenance discipline reused: meta.source authored|derived, a derived replace never touches an authored row. pcb_netconns itself is never rewritten by a swap.';


--
-- Name: pcb_pin_swaps_swap_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_pin_swaps ALTER COLUMN swap_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_pin_swaps_swap_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_pins; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_pins (
    pin_id bigint NOT NULL,
    component_id bigint NOT NULL,
    pad text,
    name text NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    description text,
    note text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    retired_at timestamp with time zone
);


--
-- Name: TABLE pcb_pins; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_pins IS 'Pins of a component type (ADR 0042) — pad + function name + electrical tags. note = LLM reasoning.';


--
-- Name: pcb_pins_pin_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_pins ALTER COLUMN pin_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_pins_pin_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_planes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_planes (
    plane_id bigint NOT NULL,
    board_id bigint NOT NULL,
    layer text NOT NULL,
    net_id bigint NOT NULL,
    region_hint jsonb,
    note text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    retired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE pcb_planes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_planes IS 'Authored plane assignment (pcb-guided-place-route) per (board, layer, net) + region_hint. Derived polygon + island report live in pcb_copper (ctype=pour) + pcb_drc_findings.';


--
-- Name: pcb_planes_plane_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_planes ALTER COLUMN plane_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_planes_plane_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_routes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_routes (
    route_id bigint NOT NULL,
    board_id bigint NOT NULL,
    net_id bigint NOT NULL,
    tree jsonb,
    topology jsonb,
    layer_assign jsonb,
    status text DEFAULT 'unrouted'::text NOT NULL,
    fail jsonb,
    note text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pcb_routes_status_chk CHECK ((status = ANY (ARRAY['unrouted'::text, 'sketched'::text, 'realized'::text, 'failed'::text])))
);


--
-- Name: TABLE pcb_routes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_routes IS 'The canonical sketch (pcb-guided-place-route) — sketch-as-canonical, copper is derived (pcb_copper). One row per (board, net); status is the legible route state machine; fail names the blocking gap.';


--
-- Name: pcb_routes_route_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_routes ALTER COLUMN route_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_routes_route_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pdf_locations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pdf_locations (
    pdf_sha256 character(64) NOT NULL,
    host text NOT NULL,
    path text NOT NULL,
    seen_at timestamp with time zone DEFAULT now() NOT NULL
);


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
-- Name: ref_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ref_embeddings (
    ref_id bigint NOT NULL,
    embedder text NOT NULL,
    embedding public.vector(1024) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
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
    retired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    auto_refresh_days integer,
    refreshed_at timestamp with time zone,
    parent_id bigint,
    prio smallint,
    handle text,
    last_viewed_at timestamp with time zone,
    alert_source text,
    fingerprint text,
    resolved_at timestamp with time zone,
    doi_status text,
    doi_validated_at timestamp with time zone,
    CONSTRAINT refs_doi_status_check CHECK (((doi_status IS NULL) OR (doi_status = ANY (ARRAY['valid'::text, 'not_found'::text])))),
    CONSTRAINT refs_pdf_role_check CHECK (((pdf_role IS NULL) OR (pdf_role = ANY (ARRAY['main'::text, 'supplement'::text, 'appendix'::text, 'front_matter'::text, 'back_matter'::text])))),
    CONSTRAINT refs_prio_check CHECK (((prio IS NULL) OR ((prio >= 1) AND (prio <= 10)))),
    CONSTRAINT refs_retraction_status_check CHECK (((retraction_status IS NULL) OR (retraction_status = ANY (ARRAY['retracted'::text, 'corrected'::text, 'expression_of_concern'::text]))))
)
WITH (fillfactor='85');


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
-- Name: resource_slot_holds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_slot_holds (
    id bigint NOT NULL,
    host text NOT NULL,
    resource text NOT NULL,
    units integer NOT NULL,
    holder text NOT NULL,
    acquired_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    holder_host text,
    holder_process text,
    holder_boot_id text,
    CONSTRAINT resource_slot_holds_units_check CHECK ((units > 0))
);


--
-- Name: TABLE resource_slot_holds; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.resource_slot_holds IS 'TTL lease per resource_slots reservation. Expired holds are swept by the heartbeat pass, refunding their units to resource_slots.free — crash-safe reclaim for a holder killed before release().';


--
-- Name: COLUMN resource_slot_holds.holder_boot_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.resource_slot_holds.holder_boot_id IS 'Worker boot epoch of the holder (see host_heartbeat.meta.boot_ids). NULL = unadvertised holder, TTL-only reclaim; non-NULL lets the reaper reclaim as soon as the generation is provably replaced.';


--
-- Name: resource_slot_holds_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.resource_slot_holds_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: resource_slot_holds_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.resource_slot_holds_id_seq OWNED BY public.resource_slot_holds.id;


--
-- Name: resource_slots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_slots (
    host text NOT NULL,
    resource text NOT NULL,
    capacity integer NOT NULL,
    free integer NOT NULL,
    kind text DEFAULT 'hard'::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_slots_capacity_check CHECK ((capacity >= 0)),
    CONSTRAINT resource_slots_free_le_capacity CHECK ((free <= capacity)),
    CONSTRAINT resource_slots_kind_check CHECK ((kind = ANY (ARRAY['hard'::text, 'soft'::text])))
)
WITH (autovacuum_vacuum_scale_factor='0', autovacuum_vacuum_threshold='200', autovacuum_vacuum_cost_delay='0');


--
-- Name: TABLE resource_slots; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.resource_slots IS 'Per-host resource offering + materialized free-slot counter. kind=hard refuses past 0 (gpu/llm), kind=soft over-commits (memory). Populated by the heartbeat self-probe; reserved at claim (slice 6c). Factory scheduler §5.';


--
-- Name: s2_neighbors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.s2_neighbors (
    ref_id bigint NOT NULL,
    direction text NOT NULL,
    ord integer NOT NULL,
    s2_id text,
    doi text,
    title text,
    year integer,
    held_ref_id bigint,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT s2_neighbors_direction_check CHECK ((direction = ANY (ARRAY['cites'::text, 'cited_by'::text])))
);


--
-- Name: scheduler_leases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scheduler_leases (
    name text NOT NULL,
    interval_s integer NOT NULL,
    next_fire_at timestamp with time zone DEFAULT now() NOT NULL,
    last_fired_at timestamp with time zone,
    last_host text,
    CONSTRAINT scheduler_leases_interval_s_check CHECK ((interval_s > 0))
)
WITH (autovacuum_vacuum_scale_factor='0', autovacuum_vacuum_threshold='200', autovacuum_vacuum_cost_delay='0');


--
-- Name: TABLE scheduler_leases; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.scheduler_leases IS 'Decentralized recurring-work lease clock — one row per folded thin-timer cadence. The conditional advance (next_fire_at <= now()) IS the lock: exactly-once minting across the fleet with no designated node. Factory scheduler §15i, slice 10.';


--
-- Name: service_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_config (
    host text NOT NULL,
    service text NOT NULL,
    prio integer DEFAULT 5 NOT NULL,
    model_pref text,
    write_level text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    actor text,
    concurrency integer,
    expires_at timestamp with time zone,
    CONSTRAINT service_config_concurrency_check CHECK (((concurrency IS NULL) OR (concurrency > 0))),
    CONSTRAINT service_config_prio_check CHECK (((prio >= 0) AND (prio <= 10)))
);


--
-- Name: TABLE service_config; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.service_config IS 'Live per-host per-service run control: prio 0=off, 1..10=claim weight; host=''*'' is the all-hosts default (exact host wins). Absent row → env/profile fallback. Factory console slice 2.';


--
-- Name: COLUMN service_config.concurrency; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.service_config.concurrency IS 'Live per-host per-service in-pass LLM-call concurrency (thread-pool width); NULL = default (1, serial). Worker clamps at a hard env ceiling regardless of this value.';


--
-- Name: COLUMN service_config.expires_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.service_config.expires_at IS 'Optional TTL for this row (the §B-2 reserve pseudo-service uses it to auto-expire a forgotten reserve); NULL = no expiry.';


--
-- Name: struct_atoms; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.struct_atoms (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    label text NOT NULL,
    element text NOT NULL,
    fa double precision NOT NULL,
    fb double precision NOT NULL,
    fc double precision NOT NULL,
    fixed smallint DEFAULT 0 NOT NULL,
    magmom double precision,
    oxidation smallint,
    hybridization text,
    added_version integer NOT NULL,
    retired_version integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE struct_atoms; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.struct_atoms IS 'ADR 0043 §4/§12: a design''s atoms — intent + current fractional position. Per-atom DERIVED outputs (force/charge) are run-scoped, not here. Never embedded.';


--
-- Name: struct_atoms_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.struct_atoms ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.struct_atoms_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: struct_bond_atoms; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.struct_bond_atoms (
    bond_id bigint NOT NULL,
    atom_id bigint NOT NULL,
    image integer[] DEFAULT '{0,0,0}'::integer[] NOT NULL
);


--
-- Name: struct_bonds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.struct_bonds (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    kind text DEFAULT 'pairwise'::text NOT NULL,
    bond_order real DEFAULT 1.0 NOT NULL,
    provenance text DEFAULT 'declared'::text NOT NULL,
    i bigint,
    j bigint,
    image integer[] DEFAULT '{0,0,0}'::integer[] NOT NULL,
    added_version integer NOT NULL,
    retired_version integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: struct_bonds_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.struct_bonds ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.struct_bonds_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: struct_frames; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.struct_frames (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    step integer NOT NULL,
    energy double precision,
    max_force double precision,
    positions jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: struct_frames_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.struct_frames ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.struct_frames_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: struct_measures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.struct_measures (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    kind text NOT NULL,
    direction text,
    goal jsonb,
    strength text DEFAULT 'gauge'::text NOT NULL,
    operands jsonb,
    embodiment jsonb,
    anchor_atom_id bigint,
    anchor_bond_id bigint,
    "for" text,
    value_derived jsonb,
    verdict text,
    retired_version integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: struct_measures_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.struct_measures ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.struct_measures_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: struct_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.struct_runs (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    fidelity text NOT NULL,
    status text DEFAULT 'succeeded'::text NOT NULL,
    model text,
    on_version integer NOT NULL,
    converged boolean DEFAULT false NOT NULL,
    n_steps integer DEFAULT 0 NOT NULL,
    energy double precision,
    max_force double precision,
    max_disp double precision,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    structure_sha text,
    cache_key text,
    final_geometry jsonb,
    provenance text DEFAULT 'computed'::text NOT NULL,
    method jsonb,
    forces jsonb,
    charges jsonb,
    CONSTRAINT struct_runs_provenance_check CHECK ((provenance = ANY (ARRAY['computed'::text, 'external'::text])))
);


--
-- Name: TABLE struct_runs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.struct_runs IS 'ADR 0043 §9/§12: one compute pass (relax/NEB/MD) over a structure design at a fixed version. Derived scalars live here, never on the mutable atom row. Energy/forces NULLable — the clean geometry rung has none.';


--
-- Name: COLUMN struct_runs.cache_key; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.struct_runs.cache_key IS 'ADR 0043 §23.16 content address: sha256(structure_sha, fidelity, model, params, code_version). Lookup key for the cache-first relax; NULL for the uncached clean rung.';


--
-- Name: COLUMN struct_runs.provenance; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.struct_runs.provenance IS 'ADR 0053 §4: computed (our relax/NEB/MD pipeline) vs external (energy sourced from an imported dataset, e.g. OC20/Materials Project).';


--
-- Name: COLUMN struct_runs.method; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.struct_runs.method IS 'ADR 0053 §4: method fingerprint for external rows (functional, cutoff_eV, kmesh, spin, pseudopotentials, dataset_doi, ...). NULL for computed rows, whose method is already model + params (0043).';


--
-- Name: COLUMN struct_runs.forces; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.struct_runs.forces IS 'Per-atom force vectors (eV/Å, cartesian), canonical-rank-indexed like final_geometry (0044): {"vectors": [[fx,fy,fz], ...], "approx": bool, "source": str}. approx=true is a cheap EMT single-point estimate (the clean rung has no calculator); approx=false is a real emt/ml relax force. NULL when neither is available.';


--
-- Name: COLUMN struct_runs.charges; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.struct_runs.charges IS 'Reserved for a future charge-bearing rung (DFT+Bader, etc.) — no backend produces partial charges today, so this is always NULL. Never fabricate a value here.';


--
-- Name: struct_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.struct_runs ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.struct_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
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
-- Name: tool_calls; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tool_calls (
    call_id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    agentlog_id bigint,
    source text,
    profile text,
    verb text NOT NULL,
    kind text,
    input_keys jsonb,
    outcome text NOT NULL,
    error_type text,
    result_count integer,
    latency_ms integer
);


--
-- Name: TABLE tool_calls; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.tool_calls IS 'Per-dispatch() telemetry: verb/kind/key-set/outcome. No payload content.';


--
-- Name: COLUMN tool_calls.input_keys; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tool_calls.input_keys IS 'Top-level input kwarg NAMES only (JSONB array) — never values or bodies.';


--
-- Name: tool_calls_call_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tool_calls_call_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: tool_calls_call_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tool_calls_call_id_seq OWNED BY public.tool_calls.call_id;


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
    retired_at,
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
-- Name: web_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.web_users (
    id bigint NOT NULL,
    login text NOT NULL,
    abbrev text NOT NULL,
    full_name text,
    email text,
    password_hash text NOT NULL,
    password_salt text NOT NULL,
    password_algo text NOT NULL,
    feed_token_sha256 text,
    disabled_at timestamp with time zone,
    last_login_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    orcid text,
    CONSTRAINT web_users_abbrev_check CHECK (((abbrev = lower(abbrev)) AND (abbrev <> ''::text))),
    CONSTRAINT web_users_email_check CHECK (((email IS NULL) OR (email = lower(email)))),
    CONSTRAINT web_users_login_check CHECK (((login = lower(login)) AND (login <> ''::text))),
    CONSTRAINT web_users_orcid_check CHECK (((orcid IS NULL) OR (orcid ~ '^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]$'::text)))
);


--
-- Name: TABLE web_users; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.web_users IS 'Fully-authorized humans for the precis-web Basic-auth gate. No roles.';


--
-- Name: COLUMN web_users.abbrev; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.web_users.abbrev IS 'Short display handle for per-user edit attribution (rendered + linked later).';


--
-- Name: COLUMN web_users.password_algo; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.web_users.password_algo IS 'scrypt-v1 | scrypt-pepper-v1 — which KDF/pepper produced password_hash.';


--
-- Name: COLUMN web_users.feed_token_sha256; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.web_users.feed_token_sha256 IS 'SHA-256 of the per-user ?t= podcast credential. NULL = no feed token minted.';


--
-- Name: COLUMN web_users.orcid; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.web_users.orcid IS 'Canonical dashed ORCID iD. The identity a nanopub this person signs is attributed to.';


--
-- Name: web_users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.web_users ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.web_users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


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
-- Name: events; Type: TABLE; Schema: vault; Owner: -
--

CREATE TABLE vault.events (
    at timestamp with time zone DEFAULT now() NOT NULL,
    who text NOT NULL,
    verb text NOT NULL,
    name text NOT NULL,
    host text,
    os_user text,
    pid integer,
    ppid integer,
    process text
);


--
-- Name: secrets; Type: TABLE; Schema: vault; Owner: -
--

CREATE TABLE vault.secrets (
    name text NOT NULL,
    ciphertext bytea NOT NULL,
    hint text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chunk_events event_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_events ALTER COLUMN event_id SET DEFAULT nextval('public.chunk_events_event_id_seq'::regclass);


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
-- Name: llm_call_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_call_log ALTER COLUMN id SET DEFAULT nextval('public.llm_call_log_id_seq'::regclass);


--
-- Name: nanopub_artifacts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_artifacts ALTER COLUMN id SET DEFAULT nextval('public.nanopub_artifacts_id_seq'::regclass);


--
-- Name: nanopub_mirror_edges id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_mirror_edges ALTER COLUMN id SET DEFAULT nextval('public.nanopub_mirror_edges_id_seq'::regclass);


--
-- Name: nanopub_ots_batches id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_ots_batches ALTER COLUMN id SET DEFAULT nextval('public.nanopub_ots_batches_id_seq'::regclass);


--
-- Name: nanopub_ots_leaves id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_ots_leaves ALTER COLUMN id SET DEFAULT nextval('public.nanopub_ots_leaves_id_seq'::regclass);


--
-- Name: nanopub_ots_proofs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_ots_proofs ALTER COLUMN id SET DEFAULT nextval('public.nanopub_ots_proofs_id_seq'::regclass);


--
-- Name: nanopub_publish id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_publish ALTER COLUMN id SET DEFAULT nextval('public.nanopub_publish_id_seq'::regclass);


--
-- Name: nanopub_trust_allowlist id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_trust_allowlist ALTER COLUMN id SET DEFAULT nextval('public.nanopub_trust_allowlist_id_seq'::regclass);


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
-- Name: resource_slot_holds id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_slot_holds ALTER COLUMN id SET DEFAULT nextval('public.resource_slot_holds_id_seq'::regclass);


--
-- Name: tags tag_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags ALTER COLUMN tag_id SET DEFAULT nextval('public.tags_tag_id_seq'::regclass);


--
-- Name: tool_calls call_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_calls ALTER COLUMN call_id SET DEFAULT nextval('public.tool_calls_call_id_seq'::regclass);


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
-- Name: app_settings app_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_settings
    ADD CONSTRAINT app_settings_pkey PRIMARY KEY (key);


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
-- Name: cad_nodes cad_nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cad_nodes
    ADD CONSTRAINT cad_nodes_pkey PRIMARY KEY (node_id);


--
-- Name: chunk_blobs chunk_blobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_blobs
    ADD CONSTRAINT chunk_blobs_pkey PRIMARY KEY (chunk_id);


--
-- Name: chunk_citations chunk_citations_chunk_marker_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_citations
    ADD CONSTRAINT chunk_citations_chunk_marker_uniq UNIQUE (chunk_id, marker);


--
-- Name: chunk_citations chunk_citations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_citations
    ADD CONSTRAINT chunk_citations_pkey PRIMARY KEY (id);


--
-- Name: chunk_claims chunk_claims_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_claims
    ADD CONSTRAINT chunk_claims_pkey PRIMARY KEY (chunk_id, artifact);


--
-- Name: chunk_embeddings chunk_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_embeddings
    ADD CONSTRAINT chunk_embeddings_pkey PRIMARY KEY (chunk_id, embedder);


--
-- Name: chunk_events chunk_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_events
    ADD CONSTRAINT chunk_events_pkey PRIMARY KEY (event_id);


--
-- Name: chunk_kinds chunk_kinds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_kinds
    ADD CONSTRAINT chunk_kinds_pkey PRIMARY KEY (slug);


--
-- Name: chunk_review chunk_review_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_review
    ADD CONSTRAINT chunk_review_pkey PRIMARY KEY (chunk_id, checker);


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
-- Name: claim_embeddings claim_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.claim_embeddings
    ADD CONSTRAINT claim_embeddings_pkey PRIMARY KEY (hub_ref_id, embedder);


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
-- Name: component_categories component_categories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.component_categories
    ADD CONSTRAINT component_categories_pkey PRIMARY KEY (category_id);


--
-- Name: component_spec_values component_spec_values_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.component_spec_values
    ADD CONSTRAINT component_spec_values_pkey PRIMARY KEY (id);


--
-- Name: component_specs component_specs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.component_specs
    ADD CONSTRAINT component_specs_pkey PRIMARY KEY (spec_id);


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
-- Name: email_account email_account_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_account
    ADD CONSTRAINT email_account_pkey PRIMARY KEY (account);


--
-- Name: email_scan email_scan_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_scan
    ADD CONSTRAINT email_scan_pkey PRIMARY KEY (account, folder, uidvalidity, uid);


--
-- Name: embedders embedders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.embedders
    ADD CONSTRAINT embedders_pkey PRIMARY KEY (name);


--
-- Name: external_rate_limits external_rate_limits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_rate_limits
    ADD CONSTRAINT external_rate_limits_pkey PRIMARY KEY (provider);


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
-- Name: llm_blob llm_blob_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_blob
    ADD CONSTRAINT llm_blob_pkey PRIMARY KEY (hash);


--
-- Name: llm_call_log llm_call_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_call_log
    ADD CONSTRAINT llm_call_log_pkey PRIMARY KEY (id);


--
-- Name: material_properties material_properties_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_properties
    ADD CONSTRAINT material_properties_pkey PRIMARY KEY (prop_id);


--
-- Name: material_values material_values_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_values
    ADD CONSTRAINT material_values_pkey PRIMARY KEY (id);


--
-- Name: nanopub_artifacts nanopub_artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_artifacts
    ADD CONSTRAINT nanopub_artifacts_pkey PRIMARY KEY (id);


--
-- Name: nanopub_artifacts nanopub_artifacts_trusty_uri_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_artifacts
    ADD CONSTRAINT nanopub_artifacts_trusty_uri_key UNIQUE (trusty_uri);


--
-- Name: nanopub_mirror_edges nanopub_mirror_edges_from_code_to_code_relation_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_mirror_edges
    ADD CONSTRAINT nanopub_mirror_edges_from_code_to_code_relation_key UNIQUE (from_code, to_code, relation);


--
-- Name: nanopub_mirror_edges nanopub_mirror_edges_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_mirror_edges
    ADD CONSTRAINT nanopub_mirror_edges_pkey PRIMARY KEY (id);


--
-- Name: nanopub_mirror nanopub_mirror_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_mirror
    ADD CONSTRAINT nanopub_mirror_pkey PRIMARY KEY (artifact_code);


--
-- Name: nanopub_ots_batches nanopub_ots_batches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_ots_batches
    ADD CONSTRAINT nanopub_ots_batches_pkey PRIMARY KEY (id);


--
-- Name: nanopub_ots_leaves nanopub_ots_leaves_batch_id_leaf_index_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_ots_leaves
    ADD CONSTRAINT nanopub_ots_leaves_batch_id_leaf_index_key UNIQUE (batch_id, leaf_index);


--
-- Name: nanopub_ots_leaves nanopub_ots_leaves_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_ots_leaves
    ADD CONSTRAINT nanopub_ots_leaves_pkey PRIMARY KEY (id);


--
-- Name: nanopub_ots_proofs nanopub_ots_proofs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_ots_proofs
    ADD CONSTRAINT nanopub_ots_proofs_pkey PRIMARY KEY (id);


--
-- Name: nanopub_publish nanopub_publish_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_publish
    ADD CONSTRAINT nanopub_publish_pkey PRIMARY KEY (id);


--
-- Name: nanopub_trust_allowlist nanopub_trust_allowlist_identity_uri_key_fingerprint_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_trust_allowlist
    ADD CONSTRAINT nanopub_trust_allowlist_identity_uri_key_fingerprint_key UNIQUE (identity_uri, key_fingerprint);


--
-- Name: nanopub_trust_allowlist nanopub_trust_allowlist_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_trust_allowlist
    ADD CONSTRAINT nanopub_trust_allowlist_pkey PRIMARY KEY (id);


--
-- Name: news_sources news_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_sources
    ADD CONSTRAINT news_sources_pkey PRIMARY KEY (source_id);


--
-- Name: news_sources news_sources_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_sources
    ADD CONSTRAINT news_sources_url_key UNIQUE (url);


--
-- Name: paper_bib_entries paper_bib_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_bib_entries
    ADD CONSTRAINT paper_bib_entries_pkey PRIMARY KEY (id);


--
-- Name: paper_bib_entries paper_bib_entries_ref_marker_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_bib_entries
    ADD CONSTRAINT paper_bib_entries_ref_marker_uniq UNIQUE (ref_id, marker);


--
-- Name: part_availability part_availability_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.part_availability
    ADD CONSTRAINT part_availability_pkey PRIMARY KEY (lcsc);


--
-- Name: part_footprints part_footprints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.part_footprints
    ADD CONSTRAINT part_footprints_pkey PRIMARY KEY (lcsc);


--
-- Name: parts parts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parts
    ADD CONSTRAINT parts_pkey PRIMARY KEY (lcsc);


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
-- Name: pcb_boards pcb_boards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_boards
    ADD CONSTRAINT pcb_boards_pkey PRIMARY KEY (board_id);


--
-- Name: pcb_components pcb_components_component_id_ref_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_components
    ADD CONSTRAINT pcb_components_component_id_ref_id_key UNIQUE (component_id, ref_id);


--
-- Name: pcb_components pcb_components_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_components
    ADD CONSTRAINT pcb_components_pkey PRIMARY KEY (component_id);


--
-- Name: pcb_copper pcb_copper_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_copper
    ADD CONSTRAINT pcb_copper_pkey PRIMARY KEY (copper_id);


--
-- Name: pcb_drc_findings pcb_drc_findings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_drc_findings
    ADD CONSTRAINT pcb_drc_findings_pkey PRIMARY KEY (finding_id);


--
-- Name: pcb_features pcb_features_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_features
    ADD CONSTRAINT pcb_features_pkey PRIMARY KEY (feature_id);


--
-- Name: pcb_instances pcb_instances_instance_id_component_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_instances
    ADD CONSTRAINT pcb_instances_instance_id_component_id_key UNIQUE (instance_id, component_id);


--
-- Name: pcb_instances pcb_instances_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_instances
    ADD CONSTRAINT pcb_instances_pkey PRIMARY KEY (instance_id);


--
-- Name: pcb_measures pcb_measures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_measures
    ADD CONSTRAINT pcb_measures_pkey PRIMARY KEY (measure_id);


--
-- Name: pcb_net_classes pcb_net_classes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_net_classes
    ADD CONSTRAINT pcb_net_classes_pkey PRIMARY KEY (class_id);


--
-- Name: pcb_netconns pcb_netconns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_netconns
    ADD CONSTRAINT pcb_netconns_pkey PRIMARY KEY (netconn_id);


--
-- Name: pcb_nets pcb_nets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_nets
    ADD CONSTRAINT pcb_nets_pkey PRIMARY KEY (net_id);


--
-- Name: pcb_pin_swaps pcb_pin_swaps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_pin_swaps
    ADD CONSTRAINT pcb_pin_swaps_pkey PRIMARY KEY (swap_id);


--
-- Name: pcb_pins pcb_pins_pin_id_component_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_pins
    ADD CONSTRAINT pcb_pins_pin_id_component_id_key UNIQUE (pin_id, component_id);


--
-- Name: pcb_pins pcb_pins_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_pins
    ADD CONSTRAINT pcb_pins_pkey PRIMARY KEY (pin_id);


--
-- Name: pcb_planes pcb_planes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_planes
    ADD CONSTRAINT pcb_planes_pkey PRIMARY KEY (plane_id);


--
-- Name: pcb_routes pcb_routes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_routes
    ADD CONSTRAINT pcb_routes_pkey PRIMARY KEY (route_id);


--
-- Name: pdf_locations pdf_locations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pdf_locations
    ADD CONSTRAINT pdf_locations_pkey PRIMARY KEY (pdf_sha256, host);


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
-- Name: ref_embeddings ref_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_embeddings
    ADD CONSTRAINT ref_embeddings_pkey PRIMARY KEY (ref_id, embedder);


--
-- Name: ref_events ref_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_events
    ADD CONSTRAINT ref_events_pkey PRIMARY KEY (event_id);


--
-- Name: ref_identifiers ref_identifiers_doi_lc; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.ref_identifiers
    ADD CONSTRAINT ref_identifiers_doi_lc CHECK (((id_kind <> 'doi'::text) OR (id_value = lower(id_value)))) NOT VALID;


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
-- Name: resource_slot_holds resource_slot_holds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_slot_holds
    ADD CONSTRAINT resource_slot_holds_pkey PRIMARY KEY (id);


--
-- Name: resource_slots resource_slots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_slots
    ADD CONSTRAINT resource_slots_pkey PRIMARY KEY (host, resource);


--
-- Name: s2_neighbors s2_neighbors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.s2_neighbors
    ADD CONSTRAINT s2_neighbors_pkey PRIMARY KEY (ref_id, direction, ord);


--
-- Name: scheduler_leases scheduler_leases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduler_leases
    ADD CONSTRAINT scheduler_leases_pkey PRIMARY KEY (name);


--
-- Name: service_config service_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_config
    ADD CONSTRAINT service_config_pkey PRIMARY KEY (host, service);


--
-- Name: struct_atoms struct_atoms_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_atoms
    ADD CONSTRAINT struct_atoms_pkey PRIMARY KEY (id);


--
-- Name: struct_bond_atoms struct_bond_atoms_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_bond_atoms
    ADD CONSTRAINT struct_bond_atoms_pkey PRIMARY KEY (bond_id, atom_id);


--
-- Name: struct_bonds struct_bonds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_bonds
    ADD CONSTRAINT struct_bonds_pkey PRIMARY KEY (id);


--
-- Name: struct_frames struct_frames_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_frames
    ADD CONSTRAINT struct_frames_pkey PRIMARY KEY (id);


--
-- Name: struct_measures struct_measures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_measures
    ADD CONSTRAINT struct_measures_pkey PRIMARY KEY (id);


--
-- Name: struct_runs struct_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_runs
    ADD CONSTRAINT struct_runs_pkey PRIMARY KEY (id);


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
-- Name: tool_calls tool_calls_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_calls
    ADD CONSTRAINT tool_calls_pkey PRIMARY KEY (call_id);


--
-- Name: web_users web_users_abbrev_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.web_users
    ADD CONSTRAINT web_users_abbrev_key UNIQUE (abbrev);


--
-- Name: web_users web_users_feed_token_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.web_users
    ADD CONSTRAINT web_users_feed_token_sha256_key UNIQUE (feed_token_sha256);


--
-- Name: web_users web_users_login_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.web_users
    ADD CONSTRAINT web_users_login_key UNIQUE (login);


--
-- Name: web_users web_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.web_users
    ADD CONSTRAINT web_users_pkey PRIMARY KEY (id);


--
-- Name: worker_logs worker_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_logs
    ADD CONSTRAINT worker_logs_pkey PRIMARY KEY (log_id);


--
-- Name: secrets secrets_pkey; Type: CONSTRAINT; Schema: vault; Owner: -
--

ALTER TABLE ONLY vault.secrets
    ADD CONSTRAINT secrets_pkey PRIMARY KEY (name);


--
-- Name: cache_state_fresh_until_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cache_state_fresh_until_idx ON public.cache_state USING btree (fresh_until) WHERE (fresh_until IS NOT NULL);


--
-- Name: cache_state_provider_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cache_state_provider_idx ON public.cache_state USING btree (provider);


--
-- Name: cad_nodes_ref_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cad_nodes_ref_id_fk_idx ON public.cad_nodes USING btree (ref_id);


--
-- Name: cad_nodes_ref_name_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX cad_nodes_ref_name_key ON public.cad_nodes USING btree (ref_id, name) WHERE (retired_at IS NULL);


--
-- Name: cad_nodes_ref_ord_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cad_nodes_ref_ord_idx ON public.cad_nodes USING btree (ref_id, ord) WHERE (retired_at IS NULL);


--
-- Name: chunk_blobs_sha256_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_blobs_sha256_idx ON public.chunk_blobs USING btree (sha256);


--
-- Name: chunk_citations_bib_entry_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_citations_bib_entry_id_idx ON public.chunk_citations USING btree (bib_entry_id);


--
-- Name: chunk_claims_reap_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_claims_reap_idx ON public.chunk_claims USING btree (artifact, claimed_at);


--
-- Name: chunk_embeddings_failed_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_embeddings_failed_idx ON public.chunk_embeddings USING btree (chunk_id, embedder) WHERE (status = 'failed'::text);


--
-- Name: chunk_embeddings_vec_hnsw_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_embeddings_vec_hnsw_idx ON public.chunk_embeddings USING hnsw (vector public.vector_cosine_ops) WHERE ((status = 'ok'::text) AND (vector IS NOT NULL));


--
-- Name: chunk_events_chunk_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_events_chunk_id_idx ON public.chunk_events USING btree (chunk_id, ts);


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
-- Name: chunks_handle_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX chunks_handle_key ON public.chunks USING btree (handle) WHERE (handle IS NOT NULL);


--
-- Name: chunks_keywords_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_keywords_gin ON public.chunks USING gin (keywords);


--
-- Name: chunks_last_seen_desc_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_last_seen_desc_idx ON public.chunks USING btree (last_seen DESC);


--
-- Name: chunks_parent_chunk_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_parent_chunk_id_idx ON public.chunks USING btree (parent_chunk_id) WHERE (parent_chunk_id IS NOT NULL);


--
-- Name: chunks_reading_order_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_reading_order_idx ON public.chunks USING btree (ref_id, parent_chunk_id, pos) WHERE (pos IS NOT NULL);


--
-- Name: chunks_ref_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_ref_id_idx ON public.chunks USING btree (ref_id);


--
-- Name: chunks_tsv_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_tsv_idx ON public.chunks USING gin (tsv);


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
-- Name: component_spec_values_component_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX component_spec_values_component_idx ON public.component_spec_values USING btree (component_ref_id);


--
-- Name: component_spec_values_source_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX component_spec_values_source_ref_idx ON public.component_spec_values USING btree (source_ref_id);


--
-- Name: component_spec_values_spec_value_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX component_spec_values_spec_value_idx ON public.component_spec_values USING btree (spec_id, value_num);


--
-- Name: component_specs_category_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX component_specs_category_idx ON public.component_specs USING btree (category_id);


--
-- Name: dream_log_behaviors_gin_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dream_log_behaviors_gin_idx ON public.dream_log USING gin (behaviors);


--
-- Name: dream_log_outcome_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dream_log_outcome_created_idx ON public.dream_log USING btree (outcome, created_at);


--
-- Name: email_scan_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX email_scan_pending_idx ON public.email_scan USING btree (account, depth) WHERE (depth < 1);


--
-- Name: embedders_one_default_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX embedders_one_default_idx ON public.embedders USING btree (is_default) WHERE (is_default = true);


--
-- Name: host_heartbeat_log_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX host_heartbeat_log_ts_idx ON public.host_heartbeat_log USING btree (ts);


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
-- Name: llm_call_log_billable_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_call_log_billable_ts_idx ON public.llm_call_log USING btree (ts DESC) WHERE ((cost_usd IS NOT NULL) AND (placement IS DISTINCT FROM 'local'::text));


--
-- Name: llm_call_log_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_call_log_ref_idx ON public.llm_call_log USING btree (ref_id) WHERE (ref_id IS NOT NULL);


--
-- Name: llm_call_log_request_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_call_log_request_hash_idx ON public.llm_call_log USING btree (request_hash);


--
-- Name: llm_call_log_response_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_call_log_response_hash_idx ON public.llm_call_log USING btree (response_hash);


--
-- Name: llm_call_log_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_call_log_ts_idx ON public.llm_call_log USING btree (ts DESC);


--
-- Name: material_values_material_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX material_values_material_idx ON public.material_values USING btree (material_ref_id);


--
-- Name: material_values_prop_value_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX material_values_prop_value_idx ON public.material_values USING btree (property_id, value_num);


--
-- Name: material_values_source_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX material_values_source_ref_idx ON public.material_values USING btree (source_ref_id);


--
-- Name: nanopub_artifacts_aida_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nanopub_artifacts_aida_idx ON public.nanopub_artifacts USING btree (aida_uri);


--
-- Name: nanopub_artifacts_publish_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nanopub_artifacts_publish_idx ON public.nanopub_artifacts USING btree (publish_id);


--
-- Name: nanopub_mirror_aida_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nanopub_mirror_aida_idx ON public.nanopub_mirror USING btree (aida_uri) WHERE (aida_uri IS NOT NULL);


--
-- Name: nanopub_mirror_edges_to_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nanopub_mirror_edges_to_idx ON public.nanopub_mirror_edges USING btree (to_code);


--
-- Name: nanopub_mirror_signer_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nanopub_mirror_signer_idx ON public.nanopub_mirror USING btree (signer) WHERE (signer IS NOT NULL);


--
-- Name: nanopub_ots_leaves_artifact_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nanopub_ots_leaves_artifact_idx ON public.nanopub_ots_leaves USING btree (artifact_id);


--
-- Name: nanopub_ots_proofs_batch_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nanopub_ots_proofs_batch_idx ON public.nanopub_ots_proofs USING btree (batch_id, state);


--
-- Name: nanopub_publish_artifact_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nanopub_publish_artifact_idx ON public.nanopub_publish USING btree (artifact_id);


--
-- Name: nanopub_publish_batch_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nanopub_publish_batch_idx ON public.nanopub_publish USING btree (batch_id);


--
-- Name: nanopub_publish_claim_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nanopub_publish_claim_ref_idx ON public.nanopub_publish USING btree (claim_ref_id);


--
-- Name: nanopub_publish_one_live_per_hub; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX nanopub_publish_one_live_per_hub ON public.nanopub_publish USING btree (claim_ref_id) WHERE (state <> ALL (ARRAY['superseded'::text, 'retracted'::text, 'rejected'::text]));


--
-- Name: nanopub_publish_state_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nanopub_publish_state_idx ON public.nanopub_publish USING btree (state);


--
-- Name: paper_bib_entries_held_ref_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX paper_bib_entries_held_ref_id_idx ON public.paper_bib_entries USING btree (held_ref_id) WHERE (held_ref_id IS NOT NULL);


--
-- Name: paper_bib_entries_unmatched_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX paper_bib_entries_unmatched_idx ON public.paper_bib_entries USING btree (ref_id) WHERE (match_conf IS NULL);


--
-- Name: parts_params_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX parts_params_gin ON public.parts USING gin (params);


--
-- Name: parts_select_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX parts_select_idx ON public.parts USING btree (jlcpcb_assemblable, basic, stock DESC) WHERE jlcpcb_assemblable;


--
-- Name: parts_tsv_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX parts_tsv_gin ON public.parts USING gin (description_tsv);


--
-- Name: patent_watches_due_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX patent_watches_due_idx ON public.patent_watches USING btree (last_run_at NULLS FIRST);


--
-- Name: pcb_boards_ref_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_boards_ref_id_fk_idx ON public.pcb_boards USING btree (ref_id);


--
-- Name: pcb_boards_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_boards_ref_idx ON public.pcb_boards USING btree (ref_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_boards_ref_name_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX pcb_boards_ref_name_key ON public.pcb_boards USING btree (ref_id, name) WHERE (retired_at IS NULL);


--
-- Name: pcb_components_ref_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_components_ref_id_fk_idx ON public.pcb_components USING btree (ref_id);


--
-- Name: pcb_components_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_components_ref_idx ON public.pcb_components USING btree (ref_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_copper_board_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_copper_board_idx ON public.pcb_copper USING btree (board_id);


--
-- Name: pcb_copper_net_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_copper_net_id_fk_idx ON public.pcb_copper USING btree (net_id);


--
-- Name: pcb_copper_route_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_copper_route_id_fk_idx ON public.pcb_copper USING btree (route_id);


--
-- Name: pcb_drc_findings_board_run_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_drc_findings_board_run_idx ON public.pcb_drc_findings USING btree (board_id, run_id);


--
-- Name: pcb_features_board_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_features_board_idx ON public.pcb_features USING btree (board_id);


--
-- Name: pcb_features_ref_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_features_ref_id_fk_idx ON public.pcb_features USING btree (ref_id);


--
-- Name: pcb_features_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_features_ref_idx ON public.pcb_features USING btree (ref_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_instances_board_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_instances_board_idx ON public.pcb_instances USING btree (board_id);


--
-- Name: pcb_instances_component_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_instances_component_idx ON public.pcb_instances USING btree (component_id);


--
-- Name: pcb_instances_ref_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_instances_ref_id_fk_idx ON public.pcb_instances USING btree (ref_id);


--
-- Name: pcb_instances_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_instances_ref_idx ON public.pcb_instances USING btree (ref_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_instances_ref_refdes_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX pcb_instances_ref_refdes_key ON public.pcb_instances USING btree (ref_id, refdes) WHERE (retired_at IS NULL);


--
-- Name: pcb_instances_roles_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_instances_roles_gin ON public.pcb_instances USING gin (roles);


--
-- Name: pcb_measures_ref_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_measures_ref_id_fk_idx ON public.pcb_measures USING btree (ref_id);


--
-- Name: pcb_measures_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_measures_ref_idx ON public.pcb_measures USING btree (ref_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_net_classes_ref_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_net_classes_ref_id_fk_idx ON public.pcb_net_classes USING btree (ref_id);


--
-- Name: pcb_net_classes_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_net_classes_ref_idx ON public.pcb_net_classes USING btree (ref_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_net_classes_ref_name_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX pcb_net_classes_ref_name_key ON public.pcb_net_classes USING btree (ref_id, name) WHERE (retired_at IS NULL);


--
-- Name: pcb_netconns_instance_component_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_netconns_instance_component_idx ON public.pcb_netconns USING btree (instance_id, component_id);


--
-- Name: pcb_netconns_instance_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_netconns_instance_idx ON public.pcb_netconns USING btree (instance_id);


--
-- Name: pcb_netconns_net_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_netconns_net_idx ON public.pcb_netconns USING btree (net_id);


--
-- Name: pcb_netconns_phys_pin_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX pcb_netconns_phys_pin_key ON public.pcb_netconns USING btree (instance_id, pin_id);


--
-- Name: pcb_netconns_pin_component_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_netconns_pin_component_idx ON public.pcb_netconns USING btree (pin_id, component_id);


--
-- Name: pcb_nets_ref_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_nets_ref_id_fk_idx ON public.pcb_nets USING btree (ref_id);


--
-- Name: pcb_nets_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_nets_ref_idx ON public.pcb_nets USING btree (ref_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_nets_ref_name_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX pcb_nets_ref_name_key ON public.pcb_nets USING btree (ref_id, name) WHERE (retired_at IS NULL);


--
-- Name: pcb_pin_swaps_board_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_pin_swaps_board_id_fk_idx ON public.pcb_pin_swaps USING btree (board_id);


--
-- Name: pcb_pin_swaps_board_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_pin_swaps_board_idx ON public.pcb_pin_swaps USING btree (board_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_pin_swaps_instance_component_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_pin_swaps_instance_component_idx ON public.pcb_pin_swaps USING btree (instance_id, component_id);


--
-- Name: pcb_pin_swaps_net_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_pin_swaps_net_id_fk_idx ON public.pcb_pin_swaps USING btree (net_id);


--
-- Name: pcb_pin_swaps_phys_pin_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX pcb_pin_swaps_phys_pin_key ON public.pcb_pin_swaps USING btree (instance_id, pin_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_pin_swaps_pin_component_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_pin_swaps_pin_component_idx ON public.pcb_pin_swaps USING btree (pin_id, component_id);


--
-- Name: pcb_pins_comp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_pins_comp_idx ON public.pcb_pins USING btree (component_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_pins_comp_name_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX pcb_pins_comp_name_key ON public.pcb_pins USING btree (component_id, name) WHERE (retired_at IS NULL);


--
-- Name: pcb_pins_comp_pad_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX pcb_pins_comp_pad_key ON public.pcb_pins USING btree (component_id, pad) WHERE ((retired_at IS NULL) AND (pad IS NOT NULL));


--
-- Name: pcb_pins_component_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_pins_component_id_fk_idx ON public.pcb_pins USING btree (component_id);


--
-- Name: pcb_pins_tags_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_pins_tags_gin ON public.pcb_pins USING gin (tags);


--
-- Name: pcb_planes_board_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_planes_board_id_fk_idx ON public.pcb_planes USING btree (board_id);


--
-- Name: pcb_planes_board_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_planes_board_idx ON public.pcb_planes USING btree (board_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_planes_board_layer_net_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX pcb_planes_board_layer_net_key ON public.pcb_planes USING btree (board_id, layer, net_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_planes_net_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_planes_net_id_fk_idx ON public.pcb_planes USING btree (net_id);


--
-- Name: pcb_routes_board_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_routes_board_idx ON public.pcb_routes USING btree (board_id);


--
-- Name: pcb_routes_board_net_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX pcb_routes_board_net_key ON public.pcb_routes USING btree (board_id, net_id);


--
-- Name: pcb_routes_net_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_routes_net_id_fk_idx ON public.pcb_routes USING btree (net_id);


--
-- Name: pdf_locations_sha_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pdf_locations_sha_idx ON public.pdf_locations USING btree (pdf_sha256);


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
-- Name: ref_events_ref_id_source_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_events_ref_id_source_ts_idx ON public.ref_events USING btree (ref_id, source, ts);


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
-- Name: ref_tags_tag_id_created_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_tags_tag_id_created_at_idx ON public.ref_tags USING btree (tag_id, created_at DESC);


--
-- Name: ref_tags_tag_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_tags_tag_id_idx ON public.ref_tags USING btree (tag_id);


--
-- Name: refs_alive_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_alive_idx ON public.refs USING btree (kind, year) WHERE (retired_at IS NULL);


--
-- Name: refs_auto_refresh_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_auto_refresh_idx ON public.refs USING btree (auto_refresh_days, refreshed_at) WHERE (auto_refresh_days IS NOT NULL);


--
-- Name: refs_handle_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX refs_handle_key ON public.refs USING btree (handle) WHERE (handle IS NOT NULL);


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
-- Name: resource_slot_holds_expires_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_slot_holds_expires_at_idx ON public.resource_slot_holds USING btree (expires_at);


--
-- Name: resource_slot_holds_host_resource_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_slot_holds_host_resource_idx ON public.resource_slot_holds USING btree (host, resource);


--
-- Name: s2_neighbors_held_ref_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX s2_neighbors_held_ref_id_idx ON public.s2_neighbors USING btree (held_ref_id) WHERE (held_ref_id IS NOT NULL);


--
-- Name: struct_atoms_ref_element_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_atoms_ref_element_idx ON public.struct_atoms USING btree (ref_id, element) WHERE (retired_version IS NULL);


--
-- Name: struct_atoms_ref_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_atoms_ref_id_fk_idx ON public.struct_atoms USING btree (ref_id);


--
-- Name: struct_atoms_ref_label_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX struct_atoms_ref_label_key ON public.struct_atoms USING btree (ref_id, label) WHERE (retired_version IS NULL);


--
-- Name: struct_bond_atoms_atom_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_bond_atoms_atom_idx ON public.struct_bond_atoms USING btree (atom_id);


--
-- Name: struct_bonds_i_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_bonds_i_idx ON public.struct_bonds USING btree (i);


--
-- Name: struct_bonds_j_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_bonds_j_idx ON public.struct_bonds USING btree (j);


--
-- Name: struct_bonds_ref_i_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_bonds_ref_i_idx ON public.struct_bonds USING btree (ref_id, i) WHERE (retired_version IS NULL);


--
-- Name: struct_bonds_ref_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_bonds_ref_id_fk_idx ON public.struct_bonds USING btree (ref_id);


--
-- Name: struct_bonds_ref_j_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_bonds_ref_j_idx ON public.struct_bonds USING btree (ref_id, j) WHERE (retired_version IS NULL);


--
-- Name: struct_frames_run_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_frames_run_idx ON public.struct_frames USING btree (run_id, step);


--
-- Name: struct_measures_anchor_atom_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_measures_anchor_atom_idx ON public.struct_measures USING btree (anchor_atom_id);


--
-- Name: struct_measures_anchor_bond_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_measures_anchor_bond_idx ON public.struct_measures USING btree (anchor_bond_id);


--
-- Name: struct_measures_ref_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_measures_ref_id_fk_idx ON public.struct_measures USING btree (ref_id);


--
-- Name: struct_measures_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_measures_ref_idx ON public.struct_measures USING btree (ref_id) WHERE (retired_version IS NULL);


--
-- Name: struct_runs_cache_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_runs_cache_idx ON public.struct_runs USING btree (cache_key, id DESC) WHERE ((cache_key IS NOT NULL) AND (status = 'succeeded'::text) AND (provenance = 'computed'::text));


--
-- Name: struct_runs_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX struct_runs_ref_idx ON public.struct_runs USING btree (ref_id, id DESC);


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
-- Name: tool_calls_error_type_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tool_calls_error_type_ts_idx ON public.tool_calls USING btree (error_type, ts DESC) WHERE (error_type IS NOT NULL);


--
-- Name: tool_calls_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tool_calls_ts_idx ON public.tool_calls USING btree (ts DESC);


--
-- Name: tool_calls_verb_kind_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tool_calls_verb_kind_ts_idx ON public.tool_calls USING btree (verb, kind, ts DESC);


--
-- Name: uq_alert_open_source_fingerprint; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_alert_open_source_fingerprint ON public.refs USING btree (alert_source, fingerprint) WHERE ((kind = 'alert'::text) AND (retired_at IS NULL) AND (resolved_at IS NULL));


--
-- Name: web_users_orcid_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX web_users_orcid_key ON public.web_users USING btree (orcid) WHERE (orcid IS NOT NULL);


--
-- Name: worker_logs_handler_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX worker_logs_handler_ts_idx ON public.worker_logs USING btree (ts) WHERE (payload ? 'handler'::text);


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
-- Name: vault_events_name_host_at_idx; Type: INDEX; Schema: vault; Owner: -
--

CREATE INDEX vault_events_name_host_at_idx ON vault.events USING btree (name, host, at DESC);


--
-- Name: chunks chunks_forbid_body_text_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER chunks_forbid_body_text_update BEFORE UPDATE ON public.chunks FOR EACH ROW WHEN (((new.text IS DISTINCT FROM old.text) AND (old.ord >= 0) AND (old.content_sha IS NULL))) EXECUTE FUNCTION public.chunks_forbid_body_text_update();


--
-- Name: nanopub_artifacts nanopub_artifacts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER nanopub_artifacts_append_only BEFORE DELETE OR UPDATE ON public.nanopub_artifacts FOR EACH ROW EXECUTE FUNCTION public.nanopub_append_only();


--
-- Name: nanopub_ots_batches nanopub_ots_batches_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER nanopub_ots_batches_append_only BEFORE DELETE OR UPDATE ON public.nanopub_ots_batches FOR EACH ROW EXECUTE FUNCTION public.nanopub_append_only();


--
-- Name: nanopub_ots_leaves nanopub_ots_leaves_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER nanopub_ots_leaves_append_only BEFORE DELETE OR UPDATE ON public.nanopub_ots_leaves FOR EACH ROW EXECUTE FUNCTION public.nanopub_append_only();


--
-- Name: nanopub_ots_proofs nanopub_ots_proofs_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER nanopub_ots_proofs_append_only BEFORE DELETE OR UPDATE ON public.nanopub_ots_proofs FOR EACH ROW EXECUTE FUNCTION public.nanopub_append_only();


--
-- Name: ref_identifiers trg_ref_identifiers_lowercase_doi; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_ref_identifiers_lowercase_doi BEFORE INSERT OR UPDATE ON public.ref_identifiers FOR EACH ROW EXECUTE FUNCTION public.ref_identifiers_lowercase_doi();


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
-- Name: cad_nodes cad_nodes_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cad_nodes
    ADD CONSTRAINT cad_nodes_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: chunk_blobs chunk_blobs_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_blobs
    ADD CONSTRAINT chunk_blobs_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.chunks(chunk_id) ON DELETE CASCADE;


--
-- Name: chunk_citations chunk_citations_bib_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_citations
    ADD CONSTRAINT chunk_citations_bib_entry_id_fkey FOREIGN KEY (bib_entry_id) REFERENCES public.paper_bib_entries(id) ON DELETE CASCADE;


--
-- Name: chunk_citations chunk_citations_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_citations
    ADD CONSTRAINT chunk_citations_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.chunks(chunk_id) ON DELETE CASCADE;


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
-- Name: chunk_events chunk_events_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_events
    ADD CONSTRAINT chunk_events_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.chunks(chunk_id) ON DELETE CASCADE;


--
-- Name: chunk_review chunk_review_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_review
    ADD CONSTRAINT chunk_review_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.chunks(chunk_id) ON DELETE CASCADE;


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
-- Name: chunks chunks_parent_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_parent_chunk_id_fkey FOREIGN KEY (parent_chunk_id) REFERENCES public.chunks(chunk_id) ON DELETE SET NULL;


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
-- Name: claim_embeddings claim_embeddings_hub_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.claim_embeddings
    ADD CONSTRAINT claim_embeddings_hub_ref_id_fkey FOREIGN KEY (hub_ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


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
-- Name: component_spec_values component_spec_values_component_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.component_spec_values
    ADD CONSTRAINT component_spec_values_component_ref_id_fkey FOREIGN KEY (component_ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: component_spec_values component_spec_values_source_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.component_spec_values
    ADD CONSTRAINT component_spec_values_source_ref_id_fkey FOREIGN KEY (source_ref_id) REFERENCES public.refs(ref_id) ON DELETE SET NULL;


--
-- Name: component_spec_values component_spec_values_spec_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.component_spec_values
    ADD CONSTRAINT component_spec_values_spec_id_fkey FOREIGN KEY (spec_id) REFERENCES public.component_specs(spec_id);


--
-- Name: component_specs component_specs_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.component_specs
    ADD CONSTRAINT component_specs_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.component_categories(category_id);


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
-- Name: llm_call_log llm_call_log_request_hash_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_call_log
    ADD CONSTRAINT llm_call_log_request_hash_fkey FOREIGN KEY (request_hash) REFERENCES public.llm_blob(hash);


--
-- Name: llm_call_log llm_call_log_response_hash_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_call_log
    ADD CONSTRAINT llm_call_log_response_hash_fkey FOREIGN KEY (response_hash) REFERENCES public.llm_blob(hash);


--
-- Name: material_values material_values_material_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_values
    ADD CONSTRAINT material_values_material_ref_id_fkey FOREIGN KEY (material_ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: material_values material_values_property_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_values
    ADD CONSTRAINT material_values_property_id_fkey FOREIGN KEY (property_id) REFERENCES public.material_properties(prop_id);


--
-- Name: material_values material_values_source_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_values
    ADD CONSTRAINT material_values_source_ref_id_fkey FOREIGN KEY (source_ref_id) REFERENCES public.refs(ref_id) ON DELETE SET NULL;


--
-- Name: nanopub_artifacts nanopub_artifacts_publish_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_artifacts
    ADD CONSTRAINT nanopub_artifacts_publish_id_fkey FOREIGN KEY (publish_id) REFERENCES public.nanopub_publish(id);


--
-- Name: nanopub_mirror_edges nanopub_mirror_edges_from_code_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_mirror_edges
    ADD CONSTRAINT nanopub_mirror_edges_from_code_fkey FOREIGN KEY (from_code) REFERENCES public.nanopub_mirror(artifact_code) ON DELETE CASCADE;


--
-- Name: nanopub_ots_leaves nanopub_ots_leaves_artifact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_ots_leaves
    ADD CONSTRAINT nanopub_ots_leaves_artifact_id_fkey FOREIGN KEY (artifact_id) REFERENCES public.nanopub_artifacts(id);


--
-- Name: nanopub_ots_leaves nanopub_ots_leaves_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_ots_leaves
    ADD CONSTRAINT nanopub_ots_leaves_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.nanopub_ots_batches(id);


--
-- Name: nanopub_ots_proofs nanopub_ots_proofs_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_ots_proofs
    ADD CONSTRAINT nanopub_ots_proofs_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.nanopub_ots_batches(id);


--
-- Name: nanopub_publish nanopub_publish_artifact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_publish
    ADD CONSTRAINT nanopub_publish_artifact_id_fkey FOREIGN KEY (artifact_id) REFERENCES public.nanopub_artifacts(id);


--
-- Name: nanopub_publish nanopub_publish_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_publish
    ADD CONSTRAINT nanopub_publish_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.nanopub_ots_batches(id);


--
-- Name: nanopub_publish nanopub_publish_claim_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nanopub_publish
    ADD CONSTRAINT nanopub_publish_claim_ref_id_fkey FOREIGN KEY (claim_ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: paper_bib_entries paper_bib_entries_held_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_bib_entries
    ADD CONSTRAINT paper_bib_entries_held_ref_id_fkey FOREIGN KEY (held_ref_id) REFERENCES public.refs(ref_id) ON DELETE SET NULL;


--
-- Name: paper_bib_entries paper_bib_entries_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_bib_entries
    ADD CONSTRAINT paper_bib_entries_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: pcb_boards pcb_boards_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_boards
    ADD CONSTRAINT pcb_boards_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: pcb_components pcb_components_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_components
    ADD CONSTRAINT pcb_components_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: pcb_copper pcb_copper_board_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_copper
    ADD CONSTRAINT pcb_copper_board_id_fkey FOREIGN KEY (board_id) REFERENCES public.pcb_boards(board_id) ON DELETE CASCADE;


--
-- Name: pcb_copper pcb_copper_net_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_copper
    ADD CONSTRAINT pcb_copper_net_id_fkey FOREIGN KEY (net_id) REFERENCES public.pcb_nets(net_id) ON DELETE CASCADE;


--
-- Name: pcb_copper pcb_copper_route_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_copper
    ADD CONSTRAINT pcb_copper_route_id_fkey FOREIGN KEY (route_id) REFERENCES public.pcb_routes(route_id) ON DELETE CASCADE;


--
-- Name: pcb_drc_findings pcb_drc_findings_board_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_drc_findings
    ADD CONSTRAINT pcb_drc_findings_board_id_fkey FOREIGN KEY (board_id) REFERENCES public.pcb_boards(board_id) ON DELETE CASCADE;


--
-- Name: pcb_features pcb_features_board_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_features
    ADD CONSTRAINT pcb_features_board_id_fkey FOREIGN KEY (board_id) REFERENCES public.pcb_boards(board_id) ON DELETE CASCADE;


--
-- Name: pcb_features pcb_features_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_features
    ADD CONSTRAINT pcb_features_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: pcb_instances pcb_instances_board_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_instances
    ADD CONSTRAINT pcb_instances_board_id_fkey FOREIGN KEY (board_id) REFERENCES public.pcb_boards(board_id) ON DELETE CASCADE;


--
-- Name: pcb_instances pcb_instances_component_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_instances
    ADD CONSTRAINT pcb_instances_component_id_fkey FOREIGN KEY (component_id) REFERENCES public.pcb_components(component_id) ON DELETE CASCADE;


--
-- Name: pcb_instances pcb_instances_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_instances
    ADD CONSTRAINT pcb_instances_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: pcb_measures pcb_measures_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_measures
    ADD CONSTRAINT pcb_measures_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: pcb_net_classes pcb_net_classes_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_net_classes
    ADD CONSTRAINT pcb_net_classes_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: pcb_netconns pcb_netconns_instance_id_component_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_netconns
    ADD CONSTRAINT pcb_netconns_instance_id_component_id_fkey FOREIGN KEY (instance_id, component_id) REFERENCES public.pcb_instances(instance_id, component_id) ON DELETE CASCADE;


--
-- Name: pcb_netconns pcb_netconns_net_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_netconns
    ADD CONSTRAINT pcb_netconns_net_id_fkey FOREIGN KEY (net_id) REFERENCES public.pcb_nets(net_id) ON DELETE CASCADE;


--
-- Name: pcb_netconns pcb_netconns_pin_id_component_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_netconns
    ADD CONSTRAINT pcb_netconns_pin_id_component_id_fkey FOREIGN KEY (pin_id, component_id) REFERENCES public.pcb_pins(pin_id, component_id) ON DELETE CASCADE;


--
-- Name: pcb_nets pcb_nets_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_nets
    ADD CONSTRAINT pcb_nets_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: pcb_pin_swaps pcb_pin_swaps_board_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_pin_swaps
    ADD CONSTRAINT pcb_pin_swaps_board_id_fkey FOREIGN KEY (board_id) REFERENCES public.pcb_boards(board_id) ON DELETE CASCADE;


--
-- Name: pcb_pin_swaps pcb_pin_swaps_instance_id_component_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_pin_swaps
    ADD CONSTRAINT pcb_pin_swaps_instance_id_component_id_fkey FOREIGN KEY (instance_id, component_id) REFERENCES public.pcb_instances(instance_id, component_id) ON DELETE CASCADE;


--
-- Name: pcb_pin_swaps pcb_pin_swaps_net_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_pin_swaps
    ADD CONSTRAINT pcb_pin_swaps_net_id_fkey FOREIGN KEY (net_id) REFERENCES public.pcb_nets(net_id) ON DELETE CASCADE;


--
-- Name: pcb_pin_swaps pcb_pin_swaps_pin_id_component_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_pin_swaps
    ADD CONSTRAINT pcb_pin_swaps_pin_id_component_id_fkey FOREIGN KEY (pin_id, component_id) REFERENCES public.pcb_pins(pin_id, component_id) ON DELETE CASCADE;


--
-- Name: pcb_pins pcb_pins_component_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_pins
    ADD CONSTRAINT pcb_pins_component_id_fkey FOREIGN KEY (component_id) REFERENCES public.pcb_components(component_id) ON DELETE CASCADE;


--
-- Name: pcb_planes pcb_planes_board_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_planes
    ADD CONSTRAINT pcb_planes_board_id_fkey FOREIGN KEY (board_id) REFERENCES public.pcb_boards(board_id) ON DELETE CASCADE;


--
-- Name: pcb_planes pcb_planes_net_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_planes
    ADD CONSTRAINT pcb_planes_net_id_fkey FOREIGN KEY (net_id) REFERENCES public.pcb_nets(net_id) ON DELETE CASCADE;


--
-- Name: pcb_routes pcb_routes_board_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_routes
    ADD CONSTRAINT pcb_routes_board_id_fkey FOREIGN KEY (board_id) REFERENCES public.pcb_boards(board_id) ON DELETE CASCADE;


--
-- Name: pcb_routes pcb_routes_net_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_routes
    ADD CONSTRAINT pcb_routes_net_id_fkey FOREIGN KEY (net_id) REFERENCES public.pcb_nets(net_id) ON DELETE CASCADE;


--
-- Name: pdf_locations pdf_locations_pdf_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pdf_locations
    ADD CONSTRAINT pdf_locations_pdf_sha256_fkey FOREIGN KEY (pdf_sha256) REFERENCES public.pdfs(pdf_sha256) ON DELETE CASCADE;


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
-- Name: ref_embeddings ref_embeddings_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_embeddings
    ADD CONSTRAINT ref_embeddings_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


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
-- Name: s2_neighbors s2_neighbors_held_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.s2_neighbors
    ADD CONSTRAINT s2_neighbors_held_ref_id_fkey FOREIGN KEY (held_ref_id) REFERENCES public.refs(ref_id) ON DELETE SET NULL;


--
-- Name: s2_neighbors s2_neighbors_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.s2_neighbors
    ADD CONSTRAINT s2_neighbors_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: struct_atoms struct_atoms_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_atoms
    ADD CONSTRAINT struct_atoms_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: struct_bond_atoms struct_bond_atoms_atom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_bond_atoms
    ADD CONSTRAINT struct_bond_atoms_atom_id_fkey FOREIGN KEY (atom_id) REFERENCES public.struct_atoms(id) ON DELETE CASCADE;


--
-- Name: struct_bond_atoms struct_bond_atoms_bond_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_bond_atoms
    ADD CONSTRAINT struct_bond_atoms_bond_id_fkey FOREIGN KEY (bond_id) REFERENCES public.struct_bonds(id) ON DELETE CASCADE;


--
-- Name: struct_bonds struct_bonds_i_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_bonds
    ADD CONSTRAINT struct_bonds_i_fkey FOREIGN KEY (i) REFERENCES public.struct_atoms(id) ON DELETE CASCADE;


--
-- Name: struct_bonds struct_bonds_j_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_bonds
    ADD CONSTRAINT struct_bonds_j_fkey FOREIGN KEY (j) REFERENCES public.struct_atoms(id) ON DELETE CASCADE;


--
-- Name: struct_bonds struct_bonds_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_bonds
    ADD CONSTRAINT struct_bonds_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: struct_frames struct_frames_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_frames
    ADD CONSTRAINT struct_frames_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.struct_runs(id) ON DELETE CASCADE;


--
-- Name: struct_measures struct_measures_anchor_atom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_measures
    ADD CONSTRAINT struct_measures_anchor_atom_id_fkey FOREIGN KEY (anchor_atom_id) REFERENCES public.struct_atoms(id) ON DELETE CASCADE;


--
-- Name: struct_measures struct_measures_anchor_bond_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_measures
    ADD CONSTRAINT struct_measures_anchor_bond_id_fkey FOREIGN KEY (anchor_bond_id) REFERENCES public.struct_bonds(id) ON DELETE CASCADE;


--
-- Name: struct_measures struct_measures_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_measures
    ADD CONSTRAINT struct_measures_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: struct_runs struct_runs_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.struct_runs
    ADD CONSTRAINT struct_runs_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

--
-- PostgreSQL database dump
--


-- Dumped from database version 17.10 (Debian 17.10-1.pgdg12+1)
-- Dumped by pg_dump version 18.6

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
agent	LLM-mediated tool call	2026-05-21 20:06:05.179981+00
user	Direct human invocation (CLI, ops)	2026-05-21 20:06:05.179981+00
system	Server-side automation: sweeps, derived state, defaults	2026-05-21 20:06:05.179981+00
chase	Citation-chase worker — automated agent that traces findings to their primary sources and flags misattributions along the chain. See docs/design/finding-chase.md.	2026-05-30 21:33:14.261241+00
dream	Dreaming worker — mints speculative acquisitions from existing findings/claims for later review.	2026-08-30 18:23:58.976219+00
weave	Quest weave pass — automated quest-graph maintenance and stitching.	2026-08-30 18:23:58.976219+00
orcid	ORCID author-discovery stub minter — creates stub author records from ORCID lookups.	2026-08-30 18:23:58.976219+00
\.


--
-- Data for Name: artifact_kinds; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.artifact_kinds (slug, target, storage, output_table, description, deprecated_at, created_at) FROM stdin;
embed:bge-m3	chunk	typed	chunk_embeddings	BGE-M3 1024-dim dense vector	\N	2026-05-30 21:33:14.261241+00
summarize:rake-lemma	chunk	typed	chunk_summaries	RAKE keyword summary (scispacy-lemmatised)	\N	2026-05-30 21:33:14.261241+00
chase_citation	ref	untyped	ref_artifacts	Citation-chase pass result (one hop or terminal)	\N	2026-05-30 21:33:14.261241+00
resolve_citation:s2	ref	untyped	ref_artifacts	Semantic Scholar metadata enrichment for stub refs	\N	2026-05-30 21:33:14.261241+00
keybert:chunks	chunk	typed	chunks	KeyBERT phrases per chunk; abbrev-aware via refs.meta[abbrevs]	\N	2026-06-05 06:56:11.586964+00
embed:tags	tag	typed	tag_embeddings	bge-m3 embeddings of every tag in use, for semantic discovery	\N	2026-06-05 16:35:39.082596+00
\.


--
-- Data for Name: chunk_kinds; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.chunk_kinds (slug, is_card, description, deprecated_at, created_at) FROM stdin;
card_combined	t	Title + authors + abstract + keywords + cite_key	\N	2026-05-21 20:06:05.179981+00
card_title	t	Title only	\N	2026-05-21 20:06:05.179981+00
card_authors	t	Normalised author list	\N	2026-05-21 20:06:05.179981+00
card_abstract	t	Abstract only	\N	2026-05-21 20:06:05.179981+00
card_meta	t	DOI / journal / year / venue	\N	2026-05-21 20:06:05.179981+00
card_keywords	t	RAKE keywords (scispacy-lemmatised, top-50)	\N	2026-05-21 20:06:05.179981+00
paragraph	f	Body paragraph	\N	2026-05-21 20:06:05.179981+00
figure	f	Figure caption + reference	\N	2026-05-21 20:06:05.179981+00
equation	f	Inline or display equation	\N	2026-05-21 20:06:05.179981+00
caption	f	Table / figure caption	\N	2026-05-21 20:06:05.179981+00
heading	f	Section heading (rarely standalone)	\N	2026-05-21 20:06:05.179981+00
references	f	Bibliography section (excluded from default embedding)	\N	2026-05-21 20:06:05.179981+00
code_symbol	f	Function / class / module body	\N	2026-05-21 20:06:05.179981+00
memory_body	f	Memory body text	\N	2026-05-21 20:06:05.179981+00
gripe_body	f	Gripe body text	\N	2026-05-21 20:06:05.179981+00
todo_body	f	Todo body text	\N	2026-05-21 20:06:05.179981+00
conv_message	f	Single message in a conversation	\N	2026-05-21 20:06:05.179981+00
qa_pair	f	Question + answer pair	\N	2026-05-21 20:06:05.179981+00
skill_overview	f	Skill overview section	\N	2026-05-21 20:06:05.179981+00
skill_input	f	Skill input description	\N	2026-05-21 20:06:05.179981+00
skill_output	f	Skill output description	\N	2026-05-21 20:06:05.179981+00
skill_example	f	Skill example	\N	2026-05-21 20:06:05.179981+00
tool_overview	f	Tool overview section	\N	2026-05-21 20:06:05.179981+00
tool_input_schema	f	Tool input schema	\N	2026-05-21 20:06:05.179981+00
tool_output_schema	f	Tool output schema	\N	2026-05-21 20:06:05.179981+00
tool_example	f	Tool example	\N	2026-05-21 20:06:05.179981+00
web_paragraph	f	Paragraph from a cached web result	\N	2026-05-21 20:06:05.179981+00
web_section	f	Section from a cached web result	\N	2026-05-21 20:06:05.179981+00
web_citation	f	Citation from a cached web result	\N	2026-05-21 20:06:05.179981+00
youtube_segment	f	YouTube transcript segment	\N	2026-05-21 20:06:05.179981+00
wolfram_query	f	Wolfram query text	\N	2026-05-21 20:06:05.179981+00
wolfram_response	f	Wolfram response text	\N	2026-05-21 20:06:05.179981+00
decision_section	f	Section of a decision log entry	\N	2026-05-21 20:06:05.179981+00
design_section	f	Section of a design document	\N	2026-05-21 20:06:05.179981+00
patent_claim	f	Individual patent claim	\N	2026-05-21 20:06:05.179981+00
patent_section	f	Patent section (description / drawings)	\N	2026-05-21 20:06:05.179981+00
project_goal	f	Project goal entry	\N	2026-05-21 20:06:05.179981+00
project_constraint	f	Project constraint entry	\N	2026-05-21 20:06:05.179981+00
project_decision_log	f	Project decision-log entry	\N	2026-05-21 20:06:05.179981+00
project_status	f	Project status entry	\N	2026-05-21 20:06:05.179981+00
project_open_question	f	Project open question	\N	2026-05-21 20:06:05.179981+00
project_milestone	f	Project milestone	\N	2026-05-21 20:06:05.179981+00
meeting_segment	f	Meeting transcript segment	\N	2026-05-21 20:06:05.179981+00
action_item	f	Action item from a meeting	\N	2026-05-21 20:06:05.179981+00
meeting_decision	f	Decision recorded in a meeting	\N	2026-05-21 20:06:05.179981+00
email_message	f	Email message body	\N	2026-05-21 20:06:05.179981+00
email_attachment_ref	f	Reference to an email attachment	\N	2026-05-21 20:06:05.179981+00
readme_section	f	README section	\N	2026-05-21 20:06:05.179981+00
commit_message	f	Commit message	\N	2026-05-21 20:06:05.179981+00
issue_comment	f	Comment on an issue	\N	2026-05-21 20:06:05.179981+00
issue_label_change	f	Label change on an issue	\N	2026-05-21 20:06:05.179981+00
issue_milestone	f	Milestone change on an issue	\N	2026-05-21 20:06:05.179981+00
research_report_summary	f	Research-report summary section	\N	2026-05-21 20:06:05.179981+00
research_report_citation	f	Research-report citation entry	\N	2026-05-21 20:06:05.179981+00
finding_body	f	Finding claim text (the measured value plus its bare conditions)	\N	2026-05-30 21:33:14.261241+00
finding_context	f	Finding setup envelope (instrument, electrode, ambient, technique, geometry)	\N	2026-05-30 21:33:14.261241+00
table	f	Markdown table emitted by Marker (skip RAKE).	\N	2026-06-04 19:55:50.15863+00
gripe_comment	f	Gripe comment / append-only timeline entry	\N	2026-08-30 18:23:58.510584+00
job_event	f	Job worker telemetry (forensics, not search)	\N	2026-08-30 18:23:58.510584+00
job_summary	f	Job completion summary (human-readable, searchable)	\N	2026-08-30 18:23:58.510584+00
pres_slide	f	Single slide of a deck (one chunk per slide). Distinct from ``paragraph`` so renderers can show slide numbers and so cross-kind search hits can be labelled as slides.	\N	2026-08-30 18:23:58.52441+00
cron_payload	f	Cron entry body — the natural-language payload that becomes the synthetic prompt to Asa when the cron fires. Searchable; embed + chunk_keywords workers index it normally.	\N	2026-08-30 18:23:58.534004+00
message_body	f	Outbound message body. The text that gets posted. Searchable so past sends can be retrieved with search(kind='message', q='...').	\N	2026-08-30 18:23:58.534004+00
flashcard_claim	f	Flashcard claim side	\N	2026-05-21 20:06:05.179981+00
flashcard_evidence	f	Flashcard evidence side	\N	2026-05-21 20:06:05.179981+00
job_result	f	Per-tick audit chunk written by the planner-coroutine when a plan_tick job finalises (verdict + summary + files). Read by the parent todo's next tick for context.	\N	2026-08-30 18:23:58.584569+00
tag_overflow	f	Long tag-value redirect chunk: when a put attempts to land a tag value longer than 80 chars in a redirectable namespace (ask-user / halt), the full value lands here and the tag becomes ``<ns>:see-chunk-<pos>``.	\N	2026-08-30 18:23:58.584569+00
aside	f	Draft aside / callout box (admonition; tcolorbox/mdframed on export).	\N	2026-08-30 18:23:58.63061+00
listing	f	Draft code listing — verbatim code payload, optional caption face.	\N	2026-08-30 18:23:58.63061+00
term	f	Glossary term — definition as face (text), {short, long, surface_forms} in meta; lives in a draft glossary subtree.	\N	2026-08-30 18:23:58.63061+00
ulist	f	Draft unordered-list container; its children are `item` chunks (renders to itemize on export).	\N	2026-08-30 18:23:58.65874+00
olist	f	Draft ordered-list container; its children are `item` chunks (renders to enumerate; meta may carry start/label style).	\N	2026-08-30 18:23:58.65874+00
item	f	Draft list item — a first-class child chunk under a `ulist`/`olist` (may itself contain nested lists / sub-paragraphs).	\N	2026-08-30 18:23:58.65874+00
edgar_section	f	One paragraph/section block of an SEC filing, labelled with its standard section via chunks.section_path + meta.item_code (e.g. Item 1A Risk Factors, 8-K Item 2.02). Distinct from ``paragraph`` so section-scoped search and the quarter-to-quarter diff can align the same section across consecutive filings.	\N	2026-08-30 18:23:58.738606+00
figure_node	f	A figure's SVG source document — the addressable source node (fn<id>). Raw markup: minted meta.no_index=true, never embedded.	\N	2026-08-30 18:23:58.749268+00
figure_vocab	f	A figure's shared vocabulary + drawing conventions — the negotiated ground truth ("green circles are foos"). Prose, embedded + searchable.	\N	2026-08-30 18:23:58.749268+00
figure_turn	f	One chat turn on a figure (user message + model reply) — the resumable session log. Prose, embedded + searchable.	\N	2026-08-30 18:23:58.749268+00
figure_notes	f	A figure's implementation notes — the model's private design log (element ids, structural scheme, conventions). Minted meta.no_index=true, never embedded; rendered behind the "Implementation notes" tab.	\N	2026-08-30 18:23:58.751849+00
card_glossary	t	Per-paper inferred reading glossary (clustered terms + one-line definitions); derived + embeddable, written by the paper_glossary worker at ord=-1000. See docs/design/reading-prep-loop.md.	\N	2026-08-30 18:23:58.767615+00
quest_log	f	Quest logbook entry — a WORM, dated, append-only ledger row (note / observation / hypothesis / result / decision / dead-end / milestone / reflection / cost) carrying entry_type + by + optional cost in meta. A milestone entry is a deed; a cost entry feeds the tote.	\N	2026-08-30 18:23:58.777147+00
mermaid_node	f	A mermaid diagram's source document — the addressable source node (mn<id>). Minted meta.no_index=true, never embedded.	\N	2026-08-30 18:23:58.780309+00
mermaid_vocab	f	A mermaid diagram's shared vocabulary + conventions — the negotiated ground truth. Prose, embedded + searchable.	\N	2026-08-30 18:23:58.780309+00
mermaid_notes	f	A mermaid diagram's private implementation notes (node ids, structure, conventions) — the model's design log. Minted no_index, not embedded.	\N	2026-08-30 18:23:58.780309+00
mermaid_turn	f	One chat turn on a mermaid diagram (user message + model reply) — the resumable session log. Prose, embedded + searchable.	\N	2026-08-30 18:23:58.780309+00
llm_review	f	LLM catalog review-log entry — a WORM, dated, append-only ledger row (published-benchmark / measured-eval / observed-telemetry / agent-review) carrying entry_type + by + provenance in meta. The ledger layer of the catalog; the tote rolls up llm_call_log alongside it (slice 3).	\N	2026-08-30 18:23:58.797184+00
claim	f	Draft claim statement — a discrete assertion under a Claims-style heading (patent claim drafting or a scientific claim list). Prose like paragraph; kept distinct so a renderer/reviewer can tell a claim from ordinary body text.	\N	2026-08-30 18:23:58.828835+00
run_log	f	Per-seed autocatpath run-log chunk — the tail of the compute child's captured stdout/stderr for one (model, seed) run. Forensics/provenance, not a search card (mirrors job_event / job_summary).	\N	2026-08-30 18:23:58.960098+00
\.


--
-- Data for Name: embedders; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.embedders (name, dim, is_default, description, deprecated_at, created_at) FROM stdin;
bge-m3	1024	t	BAAI/bge-m3, dense; 1024-dim; multilingual	\N	2026-05-21 20:06:05.179981+00
\.


--
-- Data for Name: kinds; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.kinds (slug, is_numeric, title, description, deprecated_at, created_at) FROM stdin;
paper	f	Paper	Research paper, addressed by cite_key	\N	2026-05-21 20:06:05.179981+00
book	f	Book	Book or monograph	\N	2026-05-21 20:06:05.179981+00
patent	f	Patent	Patent document	\N	2026-05-21 20:06:05.179981+00
research_report	f	Research report	Research / industry report	\N	2026-05-21 20:06:05.179981+00
oracle	f	Oracle	Oracle / authority node	\N	2026-05-21 20:06:05.179981+00
skill	f	Skill	Agent skill document	\N	2026-05-21 20:06:05.179981+00
tool	f	Tool	Tool spec or interface description	\N	2026-05-21 20:06:05.179981+00
code	f	Code symbol	Function, class, module, or repo symbol	\N	2026-05-21 20:06:05.179981+00
decision	f	Decision	ADR-style decision log entry	\N	2026-05-21 20:06:05.179981+00
design	f	Design	Design document / plan	\N	2026-05-21 20:06:05.179981+00
project	f	Project	Project descriptor (goals, status, …)	\N	2026-05-21 20:06:05.179981+00
conv	f	Conversation	Conversation transcript	\N	2026-05-21 20:06:05.179981+00
meeting	f	Meeting	Meeting notes / transcript	\N	2026-05-21 20:06:05.179981+00
email	f	Email	Email message or thread	\N	2026-05-21 20:06:05.179981+00
repo	f	Repo	Source-code repository	\N	2026-05-21 20:06:05.179981+00
issue	f	Issue	Issue tracker item	\N	2026-05-21 20:06:05.179981+00
todo	t	Todo	Task / action item	\N	2026-05-21 20:06:05.179981+00
memory	t	Memory	Note, decision, idea, claim	\N	2026-05-21 20:06:05.179981+00
gripe	t	Gripe	Informal log entry	\N	2026-05-21 20:06:05.179981+00
web	f	Web query	Cached web / research / think query	\N	2026-05-21 20:06:05.179981+00
youtube	f	YouTube	Cached YouTube transcript	\N	2026-05-21 20:06:05.179981+00
math	f	Math result	Cached Wolfram math result	\N	2026-05-21 20:06:05.179981+00
finding	t	Finding	A retrievable empirical claim with explicit setup context and a provenance chain back to its primary source. Synthesised by the citation-chase worker; never externally citable (see docs/design/finding-chase.md).	\N	2026-05-30 21:33:14.261241+00
citation	t	Citation	Verified claim → source pointer. Written by the citation-fill workflow after the verifier confirms the source quote supports the claim.	\N	2026-05-31 14:47:51.530091+00
markdown	f	Markdown file	Read / write .md / .markdown files under a configured root. Slug derived from path; lazy re-ingest on stale mtime; block slugs are content-stable. See src/precis/handlers/markdown.py.	\N	2026-06-04 19:55:50.290874+00
plaintext	f	Plaintext file	Read / write .txt / .org / .rst files under a configured root. The shared file-kind base; markdown and tex are subclasses. See src/precis/handlers/plaintext.py.	\N	2026-06-04 19:55:50.290874+00
tex	f	LaTeX file	Read / write .tex files under a configured root. Inherits the plaintext file-kind machinery; adds tex-aware block parsing + input-resolution. See src/precis/handlers/tex.py.	\N	2026-06-04 19:55:50.290874+00
websearch	f	Web search	Cached perplexity-style web search response. Slug derived from the canonical query + model + freshness window. See src/precis/handlers/perplexity.py.	\N	2026-06-04 20:01:59.625687+00
job	t	Job	Offline run of a task — fix this gripe, run a simulation, benchmark a commit. Addressable by numeric id; status via STATUS: tags; comment timeline via job_event + job_summary chunks.	\N	2026-08-30 18:23:58.510584+00
pres	f	Presentation	Slide deck, unpublished writeup, or other internal document we want indexed but kept separate from the academic paper library. Slug-addressed; one block per slide (or per paragraph for writeups). Subtype carried as ``subtype:slides|writeup|notes|...`` open tag; ``venue`` and ``date`` live in meta. See ``precis-pres-help``.	\N	2026-08-30 18:23:58.52441+00
cron	t	Cron	Scheduled wakeup. The cron-tick CLI scans due entries every 60s, fires pg_notify('precis.cron'), advances next_fire_at per recurrence + catch_up policy. Numeric-id; body lives as a ``cron_payload`` chunk. State in meta.next_fire_at, meta.recurring, meta.catch_up, meta.status. See ``precis-cron-help``.	\N	2026-08-30 18:23:58.534004+00
message	t	Message	Proactive outbound. put(kind='message', target='discord/G/C/T', text='...') stores the ref AND fires pg_notify('precis.messages'). Delivery layer (asa_bot) LISTENs and posts. Numeric-id; one ref per send. Body as ``message_body`` chunk. State in meta.status: 'queued' → 'sent'/'failed'. See ``precis-message-help``.	\N	2026-08-30 18:23:58.534004+00
flashcard	t	Flashcard	Spaced-repetition flashcard	\N	2026-05-21 20:06:05.179981+00
perplexity-reasoning	f	Think	Cached perplexity ``think`` (chain-of-thought) response. Slug derived from the question + model + freshness window. See src/precis/handlers/perplexity.py.	\N	2026-06-04 20:01:59.625687+00
perplexity-research	f	Research report	Cached perplexity ``research`` (deep-research) response. Slug derived from the prompt + model + freshness window. See src/precis/handlers/perplexity.py.	\N	2026-06-04 20:01:59.625687+00
wikipedia	f	Wikipedia (on-demand article fetch)	Resolve a query to the best-matching Wikipedia article via the MediaWiki search API, then fetch and cache its plain-text extract. Slug-addressed by query; cached 7 days; block-split + embedded so search(kind='wikipedia', q=...) lands hits inside fetched articles. On-demand — no bulk dump, always current. See ``precis-wikipedia-help``.	\N	2026-08-30 18:23:58.608918+00
alert	t	Alert	Machine-detected operational / health condition — a worker spin loop, an orphaned todo, a stalled recurring, a stale claim. Addressable by numeric id; deduped on meta.fingerprint; lifecycle via STATUS: tags (open / resolved); source + severity via alert-source: / severity: open tags. Not embedded — surfaced by the /alerts web tab, not semantic search.	\N	2026-08-30 18:23:58.623333+00
draft	f	Draft	Editable, chunk-native authored document (ADR 0032). The living source of a project's write-up; exports to LaTeX/PDF/Word with Postgres canonical. Body chunks are mutable in structure (reorder/reparent via pos + parent_chunk_id) and in text (via the edit helper + content_sha re-derive). Named ref; chunks addressed by an opaque ¶<handle>. One draft per project; freeze = snapshot. See precis-draft-help.	\N	2026-08-30 18:23:58.63061+00
news	f	News	Multi-source news aggregation. Articles pulled from RSS/Atom feeds (the news_sources registry) by the news_poll worker, fetched + extracted + embedded like web pages, so search(kind='news', q=...) lands hits inside article bodies. URL-addressed, pinned in cache. Tagged category:news + source:<slug> for filtering. The morning briefing summarizes recent items back out. See ``precis-news-help``.	\N	2026-08-30 18:23:58.638075+00
agentlog	t	Agent log	Run-attribution record — one per agentic run (plan_tick, operator change request, chat follow-up) that touches the corpus. Carries the full assembled prompt, model + source, and `touched` links to every chunk the run wrote or moved, so a suspicious chunk can be walked back to the run that produced it. Numeric id; deduped per run; GC'd past a retention window (links drop, chunks stay). Not embedded — surfaced by the /agentlogs web tab and chunk connections, not semantic search. See ``precis-agentlog-help``.	\N	2026-08-30 18:23:58.643936+00
orcid	f	ORCID author	A researcher identity resolved from ORCID (https://orcid.org). Slug-addressed by iD (e.g. 'orcid:0000-0002-1825-0097'). get resolves + stores the record (names, bio, keywords, employments with ROR ids), links works already held, and reports the missing ones — fetching them is LLM-gated via args={'enqueue': N}; search runs over the embedded author card; link/tag attach authorship edges (authored / authored-by) and classification. Durable link hub — never cache-evicted. See ``precis-orcid-help``.	\N	2026-08-30 18:23:58.670152+00
cad	f	CAD	Parametric solid-model design (ADR 0041) — a boolean DAG of placed analytic primitives (box/cyl/cone/sphere/torus/prism/pyramid) authored via the compact `config` mini-DSL (e.g. cyl:r3h12). Postgres-canonical; the agent probes the model (point/ray/arc/section) and relates whole parts (clearance/interference/translational DOF) analytically rather than meshing. OpenSCAD/STL export is a regenerable downstream view. Named ref; nodes addressed by an opaque ca<id> handle. See precis-cad-help.	\N	2026-08-30 18:23:58.674923+00
structure	f	Structure	Atomistic cell + bond-graph design for DFT/molecular modelling (ADR 0043). A periodic cell (lattice + per-axis PBC) filled with atoms (a<El><n> labels) and an explicit bond graph (order + provenance + periodic-image offset). The agent edits the graph via typed ops and probes it analytically (neighbours, coordination, MIC distances/angles, a validator gate) in memory — never pixels. Relaxation/DFT and file export (CIF/POSCAR/XYZ) are rented backends. Postgres-canonical; st<id> handle, design-scoped atom paths st<id>#a<El><n>. See precis-structure-help.	\N	2026-08-30 18:23:58.678891+00
pcb	f	PCB	Electronics/PCB design (ADR 0042) — a netlist + placement graph in dedicated tables, read and authored by the LLM as a traversable graph (ratsnest / measures / signal-trace), never pixels. JLCPCB-native. Postgres-canonical; Freerouting/gerbers/fab are downstream export. See precis-pcb-help.	\N	2026-08-30 18:23:58.704982+00
part	f	Part	LCSC/JLCPCB catalog part (ADR 0042) — reference data in the `parts` table, addressed by LCSC C-number. Ingest-only (jlcparts dump); not embedded. See precis-part-select-help.	\N	2026-08-30 18:23:58.704982+00
datasheet	f	Datasheet	Component datasheet (ADR 0042) — a thin PaperHandler sibling (corpus_role=evidence) ingested via the Marker->chunks pipeline and linked datasheet-of a part. One kind for the whole electronics-doc family (app-note/errata via a meta sub-type). See precis-datasheet-help.	\N	2026-08-30 18:23:58.704982+00
folder	t	Folder	Organizational container (ADR 0045): single-parent placement for authored artifacts via refs.parent_id and the reserved virtual `parent` link relation (ADR 0027, generalized). Folders organize what you MAKE — corpus kinds (paper/cfp) keep their own discovery layer and stream kinds (memory/alert/job) stay out. Shallow by policy. See precis-folder-help.	\N	2026-08-30 18:23:58.718003+00
edgar	f	SEC Filing	Read-only SEC EDGAR filing (10-K / 10-Q / 8-K / S-1 / …). Accession-slugged (e.g. 0000320193-23-000106). Search merges local + EDGAR full-text; get(id=...) fetches the submissions index + primary document and stores section-labelled blocks. get(id='cik:320193' | 'ticker:aapl') lists a company's recent filings; view='diff' shows quarter-to-quarter section changes. See ``precis-edgar-help``.	\N	2026-08-30 18:23:58.738606+00
plan	f	Plan	A thread's reasoning outline (ADR 0051 §2b) — a hierarchical todo-list + notes on the same chunk-tree substrate as a draft, addressed by pe<chunk_id>. Rendered whole with [open]/[wip]/done: status markers + a cursor; NEVER exported as a deliverable (corpus_role=none). One plan per project (plan-of link). See precis-overview.	\N	2026-08-30 18:23:58.746955+00
figure	f	Figure	An interactive SVG canvas you draw *with* the model — a slug-addressed chunk-tree on the draft substrate, addressed by fg<ref>/fn<chunk>. Two model-owned documents: the SVG source (figure_node chunks) + a shared vocabulary (figure_vocab); chat persists as figure_turn. NEVER exported as a deliverable (corpus_role=none). Many per project (figure-of link). See precis-figure-help.	\N	2026-08-30 18:23:58.749268+00
anki	t	Anki card	A spaced-repetition cloze card ({{c1::…}}) that lives in the corpus and syncs to AnkiWeb. Numeric-id ref; body is cloze markup, meta carries the generic Anki note shape (notetype/deck/fields). Anki owns scheduling — no SM-2 here. Supersedes flashcard. See precis-anki-help.	\N	2026-08-30 18:23:58.761456+00
concept	t	Concept	A node in the learner's personal knowledge graph (reading-prep loop): a term/idea with a continuous mastery field, derived state, embeddable definition, and typed edges (prerequisite / analogy / contrast) to other concepts. Objectives are concepts, not todos. See reading-prep-loop.md.	\N	2026-08-30 18:23:58.770419+00
quest	t	Quest	A perpetual, unachievable striving (the medieval Grail sense) that pulls subtasks and knowledge acquisition into its service. Never `done` — lifecycle is active/dormant/abandoned. Achievable goals beneath it are ordinary todos/projects marked `serves`. Progress is a ledger of deeds, not a percentage. See docs/proposals/quest-layer.md.	\N	2026-08-30 18:23:58.777147+00
mermaid	f	Mermaid	A mermaid diagram you draw *with* the model — a slug-addressed chunk-tree on the draft substrate, addressed by mm<ref>/mn<chunk>. Model-owned: the mermaid source (mermaid_node) + a shared vocabulary (mermaid_vocab) + private notes (mermaid_notes); chat persists as mermaid_turn. Nodes bind to the chunks they depict (ADR 0057). NEVER exported (corpus_role=none). Many per project (mermaid-of link). See precis-mermaid-help.	\N	2026-08-30 18:23:58.780309+00
llm	t	LLM catalog	A model catalog card — one ref per model (claude-opus-4-8, qwen-heavy). Body is the capability prose (embedded, so the card is a vector); meta carries the structured facts (model_id, tier_floor, offerings, capability axes, provenance). A reconcile pass keeps the facts true against the live OpenRouter feed and flags drift. Read with get(kind='llm', id='claude-opus-4-8') or search(kind='llm', q=…). Never exported. See docs/proposals/llm-catalog.md.	\N	2026-08-30 18:23:58.797184+00
material	f	Material	CRC-handbook-style engineering material properties store — a slug entity (name/aliases/class) plus per-property sourced values in a typed, growable property registry. v1 is canonical-units-only: a unit that is not the property's canonical unit is rejected, named. See precis-material-help.	\N	2026-08-30 18:23:58.859366+00
component	f	Component	General procurable-part store — a slug entity (name/category/mpn/manufacturer) plus per-spec sourced values in a typed, growable, category-scoped spec registry. made-of links a component to the material it is made of. v1 is canonical-units-only, like material. See precis-component-help.	\N	2026-08-30 18:23:58.865641+00
cfp	f	Call for Proposal	Call-for-proposal / requirements document. A read-only ingested PDF (via `precis add --as cfp` or the inbox/cfp/ watch dir) that a proposal draft must satisfy. Addressable by slug; one ref per document, blocks per chunk — gets search / TOC / keywords like a paper. Spec role: NEVER citable evidence (it is the requirements, not a source). Link it to a proposal project with link(rel='has-requirement') so the planner consults it. Use get(view='toc') to read the required sections + limits.	\N	2026-08-30 18:23:58.952034+00
\.


--
-- Data for Name: providers; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.providers (slug, description, deprecated_at, created_at) FROM stdin;
arxiv	arXiv preprint server	\N	2026-05-21 20:06:05.179981+00
crossref	Crossref DOI metadata	\N	2026-05-21 20:06:05.179981+00
s2	Semantic Scholar	\N	2026-05-21 20:06:05.179981+00
pubmed	PubMed / NCBI	\N	2026-05-21 20:06:05.179981+00
openalex	OpenAlex	\N	2026-05-21 20:06:05.179981+00
unpaywall	Unpaywall OA index	\N	2026-05-21 20:06:05.179981+00
perplexity	Perplexity (web / research / think)	\N	2026-05-21 20:06:05.179981+00
wolfram	Wolfram Alpha math	\N	2026-05-21 20:06:05.179981+00
youtube	YouTube transcript	\N	2026-05-21 20:06:05.179981+00
manual	Manually uploaded	\N	2026-05-21 20:06:05.179981+00
local	Local computation / no external source	\N	2026-05-21 20:06:05.179981+00
retraction_watch	Retraction Watch dataset (CC-BY via Crossref)	\N	2026-05-30 16:07:11.520836+00
web	Direct web fetch / trafilatura extraction	\N	2026-05-31 18:20:12.906601+00
epo_ops	European Patent Office Open Patent Services REST API	\N	2026-06-04 20:02:44.133862+00
wikipedia	Wikipedia / MediaWiki API (search + plain-text extracts)	\N	2026-08-30 18:23:58.608918+00
news	RSS / Atom news feeds (news_sources registry)	\N	2026-08-30 18:23:58.638075+00
orcid	ORCID Public API (https://pub.orcid.org/v3.0/) — author identity + works	\N	2026-08-30 18:23:58.670152+00
sec_edgar	US SEC EDGAR — company filings (submissions + archive APIs)	\N	2026-08-30 18:23:58.738606+00
sec_edgar_search	US SEC EDGAR — full-text search (efts.sec.gov)	\N	2026-08-30 18:23:58.738606+00
markup	Structured full-text ingest (JATS / Elsevier XML / arXiv HTML / LaTeX)	\N	2026-08-30 18:23:58.79156+00
\.


--
-- Data for Name: relations; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.relations (slug, is_symmetric, inverse_slug, description, deprecated_at, created_at) FROM stdin;
related-to	t	\N	Symmetric association	\N	2026-05-21 20:06:05.179981+00
blocks	f	blocked-by	Source blocks target	\N	2026-05-21 20:06:05.179981+00
blocked-by	f	blocks	Source is blocked by target	\N	2026-05-21 20:06:05.179981+00
contradicts	f	contradicted-by	Source contradicts target	\N	2026-05-21 20:06:05.179981+00
contradicted-by	f	contradicts	Source is contradicted by target	\N	2026-05-21 20:06:05.179981+00
cites	f	cited-by	Source cites target	\N	2026-05-21 20:06:05.179981+00
cited-by	f	cites	Source is cited by target	\N	2026-05-21 20:06:05.179981+00
supersedes	f	superseded-by	Source supersedes target	\N	2026-05-21 20:06:05.179981+00
superseded-by	f	supersedes	Source is superseded by target	\N	2026-05-21 20:06:05.179981+00
retracted-by	f	retracts	Source is retracted by target (retraction notice)	\N	2026-05-30 16:07:11.520836+00
retracts	f	retracted-by	Source retracts target	\N	2026-05-30 16:07:11.520836+00
corrected-by	f	corrects	Source is corrected by target (corrigendum/erratum/addendum)	\N	2026-05-30 16:07:11.520836+00
corrects	f	corrected-by	Source corrects target	\N	2026-05-30 16:07:11.520836+00
concern-raised-by	f	raises-concern-about	Source has an Expression of Concern attached	\N	2026-05-30 16:07:11.520836+00
raises-concern-about	f	concern-raised-by	Source raises concern about target	\N	2026-05-30 16:07:11.520836+00
misattributes	f	misattributed-by	Source chunk misrepresents what the target chunk actually says	\N	2026-05-30 21:33:14.261241+00
misattributed-by	f	misattributes	Source chunk is misrepresented by the linked source chunk	\N	2026-05-30 21:33:14.261241+00
derived-from	f	derived-into	Source is derived from target (cause/origin)	\N	2026-05-31 18:20:12.906601+00
derived-into	f	derived-from	Source is the origin from which target derives	\N	2026-05-31 18:20:12.906601+00
supports	f	supported-by	Source provides evidence for target	\N	2026-05-31 18:20:12.906601+00
supported-by	f	supports	Source is supported by target	\N	2026-05-31 18:20:12.906601+00
generalises	f	specialises	Source is a generalisation of target	\N	2026-05-31 18:20:12.906601+00
specialises	f	generalises	Source is a specialisation of target	\N	2026-05-31 18:20:12.906601+00
see-also	f	\N	One-way "for context" pointer (no inverse)	\N	2026-05-31 18:20:12.906601+00
fixes	f	fixed-by	Source ref offers a fix for the target ref (e.g. a fix_gripe job → its gripe)	\N	2026-08-30 18:23:58.513644+00
fixed-by	f	fixes	Source ref is being fixed by the target ref	\N	2026-08-30 18:23:58.513644+00
draft-of	f	has-draft	Source draft is the working document of target project (todo).	\N	2026-08-30 18:23:58.635027+00
has-draft	f	draft-of	Source project (todo) has target draft as its working document.	\N	2026-08-30 18:23:58.635027+00
snapshot-of	f	has-snapshot	Source frozen ref is a point-in-time snapshot of target draft.	\N	2026-08-30 18:23:58.635027+00
has-snapshot	f	snapshot-of	Source draft has target frozen ref as a snapshot.	\N	2026-08-30 18:23:58.635027+00
touched	t	\N	Source agent run wrote or moved target chunk (run-attribution). Symmetric for graph purposes — surfaced from either end.	\N	2026-08-30 18:23:58.643936+00
plots	f	plotted-by	Source figure chunk renders the target data chunk — the figure plots that data. The one reactive edge: editing the data marks the figure stale (ADR 0035).	\N	2026-08-30 18:23:58.661966+00
plotted-by	f	plots	Source data chunk is rendered by the target figure chunk (inverse of plots).	\N	2026-08-30 18:23:58.661966+00
authored	f	authored-by	Source author node (kind=orcid) authored the target paper. Ref-level edge; meta carries best-effort author_position / n_authors when known (ADR 0039).	\N	2026-08-30 18:23:58.66753+00
authored-by	f	authored	Source paper was authored by the target author node (inverse of authored).	\N	2026-08-30 18:23:58.66753+00
has-requirement	f	requirement-of	Source project (todo) must satisfy target call-for-proposal (cfp).	\N	2026-08-30 18:23:58.672473+00
requirement-of	f	has-requirement	Source call-for-proposal (cfp) is a requirement of target project.	\N	2026-08-30 18:23:58.672473+00
requested	f	requested-by	Source todo requested target derived job and waits on it.	\N	2026-08-30 18:23:58.700402+00
requested-by	f	requested	Source derived job was requested by target todo.	\N	2026-08-30 18:23:58.700402+00
datasheet-of	f	has-datasheet	Source datasheet documents target part (evidence for its specs).	\N	2026-08-30 18:23:58.742485+00
has-datasheet	f	datasheet-of	Source part is documented by target datasheet.	\N	2026-08-30 18:23:58.742485+00
plan-of	f	has-plan	Source plan is the reasoning outline of target project (todo).	\N	2026-08-30 18:23:58.746955+00
has-plan	f	plan-of	Source project (todo) has target plan as its reasoning outline.	\N	2026-08-30 18:23:58.746955+00
figure-of	f	has-figure	Source figure belongs to target project (todo). Many-per-project.	\N	2026-08-30 18:23:58.749268+00
has-figure	f	figure-of	Source project (todo) has target figure. Many-per-project.	\N	2026-08-30 18:23:58.749268+00
has-prerequisite	f	prerequisite-of	Source concept requires target concept first (the learning DAG).	\N	2026-08-30 18:23:58.770419+00
prerequisite-of	f	has-prerequisite	Source concept is a prerequisite of (must be learned before) target.	\N	2026-08-30 18:23:58.770419+00
analogy-of	t	\N	Source and target concepts are analogous — teach one via the other.	\N	2026-08-30 18:23:58.770419+00
contrasts-with	t	\N	Source and target concepts are confusably similar but distinct.	\N	2026-08-30 18:23:58.770419+00
represents	f	represented-by	Source concept is rendered by target card (an anki/other representation).	\N	2026-08-30 18:23:58.770419+00
represented-by	f	represents	Source card renders (is a representation of) target concept.	\N	2026-08-30 18:23:58.770419+00
depicts	f	depicted-in	A diagram (figure/mermaid) source chunk depicts the target chunk/ref it illustrates; the depicting element id(s) live in links.meta.elements. Diagram→corpus binding (ADR 0057), the element-granular cousin of plots.	\N	2026-08-30 18:23:58.773612+00
depicted-in	f	depicts	Source chunk/ref is depicted by the target diagram (inverse of depicts, ADR 0057).	\N	2026-08-30 18:23:58.773612+00
serves	f	served-by	Source (project/todo/concept/paper/job/draft/structure/sub-quest) is in the service of the target quest — the striving DAG above the todo tree.	\N	2026-08-30 18:23:58.777147+00
served-by	f	serves	Source quest is served by the target work/knowledge node.	\N	2026-08-30 18:23:58.777147+00
mermaid-of	f	has-mermaid	Source mermaid diagram belongs to target project (todo). Many-per-project.	\N	2026-08-30 18:23:58.780309+00
has-mermaid	f	mermaid-of	Source project (todo) has target mermaid diagram. Many-per-project.	\N	2026-08-30 18:23:58.780309+00
dossier-of	f	has-dossier	Source draft is the research dossier of the target quest — the living synthesis rewritten each cycle, and the loop's rolling context.	\N	2026-08-30 18:23:58.785877+00
has-dossier	f	dossier-of	Source quest has the target draft as its research dossier.	\N	2026-08-30 18:23:58.785877+00
entails	f	entailed-by	Source inference node logically yields the target conclusion lemma (asserted, not proven).	\N	2026-08-30 18:23:58.822133+00
entailed-by	f	entails	Source lemma is the asserted conclusion of the target inference node.	\N	2026-08-30 18:23:58.822133+00
qualifies	f	qualified-by	Source caveat node limits/bounds the target claim (finding or lemma).	\N	2026-08-30 18:23:58.822133+00
qualified-by	f	qualifies	Source claim is limited/bounded by the target caveat node.	\N	2026-08-30 18:23:58.822133+00
cited-in	f	\N	Paper is woven into and cited by the document; a citation exists. src=paper, dst=dossier draft (optionally its section chunk).	\N	2026-08-30 18:23:58.835375+00
corroborates	f	\N	Paper supports an existing point in the document, grouped with it.	\N	2026-08-30 18:23:58.835375+00
superseded-in	f	\N	Paper is subsumed by a later or review paper already integrated; recorded, not separately woven.	\N	2026-08-30 18:23:58.835375+00
off-topic-for	f	\N	Paper was considered for the document and rejected as out of scope.	\N	2026-08-30 18:23:58.835375+00
copy-of	f	has-copy	Source draft is a fork/deep-copy of target draft (chunks + links copied).	\N	2026-08-30 18:23:58.847481+00
has-copy	f	copy-of	Source draft has target draft as a fork/deep-copy of itself.	\N	2026-08-30 18:23:58.847481+00
paper-of	f	has-paper	Source draft is the reader-facing paper projection of the target quest/process's dossier — a separate draft from the dossier itself.	\N	2026-08-30 18:23:58.850409+00
has-paper	f	paper-of	Source quest/process has the target draft as its reader-facing paper.	\N	2026-08-30 18:23:58.850409+00
made-of	f	used-in	Source component is made of target material.	\N	2026-08-30 18:23:58.865641+00
used-in	f	made-of	Source material is used in target component.	\N	2026-08-30 18:23:58.865641+00
establishes	f	\N	Source paper first showed / originated the target claim (taproot evidence edge; originator).	\N	2026-08-30 18:23:58.876244+00
contains	f	part-of	Source component structurally contains target component (BOM edge).	\N	2026-08-30 18:23:58.879876+00
part-of	f	contains	Source component is structurally part of target component.	\N	2026-08-30 18:23:58.879876+00
refines	f	\N	Source claim hub is a sharper/reworded version of the target claim hub (taproot claim→claim advisory link; link-don't-merge, no evidence flow).	\N	2026-08-30 18:23:58.896491+00
awaits-evidence	f	\N	An acquisition-mode finding (STATUS:acquiring) awaits corpus evidence from the linked DREAM:acquire paper stub.	\N	2026-08-30 18:23:58.91107+00
same-family-as	t	\N	Both patent refs are members of the same EPO OPS DOCDB patent family; source is typically a stub ingest, target the family's current publication-date representative.	\N	2026-08-30 18:23:58.941741+00
conjunct-of	f	\N	Source claim hub is one atomic conjunct of the target compound claim hub (taproot claim→claim advisory link; link-don't-merge, no evidence flow).	\N	2026-08-30 18:23:58.973866+00
motivated-by	f	\N	Source hypothesis claim hub was provoked by the target artifact (paper, patent, or claim hub) — taproot advisory link; motivation, NOT evidence, and no evidence flows along it.	\N	2026-08-30 18:23:59.016758+00
tests	f	\N	Source measurement artifact (computed pathway) executed the target hypothesis finding's pre-registered discriminating experiment — quest dialectic measurement-ruling edge; NOT evidence, and no evidence flows along it (sim rulings settle internal hypotheses only).	\N	2026-08-30 18:23:59.065835+00
\.


--
-- Data for Name: summarizers; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.summarizers (name, prompt_template, config, is_default, description, deprecated_at, created_at) FROM stdin;
rake-lemma	\N	{"model": "en_core_sci_sm", "lemmatizer": "scispacy", "max_keywords": 50, "max_phrase_words": 4, "min_phrase_words": 1}	t	RAKE phrase extraction + scispacy lemmatisation	\N	2026-05-21 20:06:05.179981+00
llm-v1	\N	{"alias": "summarizer", "model": "qwen3-next-80b-a3b", "format": "brief;detail", "version": "1", "endpoint": "local"}	f	LLM brief+detail chunk summary (Qwen3-Next-80B-A3B via the litellm `summarizer` alias)	\N	2026-08-30 18:23:58.606503+00
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
0029_alert_kind	1970-01-01 00:00:00+00	e0f0a86a8e594808753c80216e23c7e929809e5d424c06645e9ba65effb378ba	precis
0030_alert_open_unique_index	1970-01-01 00:00:00+00	149760b4ce4ac7f295ee91189c72a6191d515aebbc5d6044e5f0634f4e2df8ef	precis
0031_draft_kind	1970-01-01 00:00:00+00	b651181991b941525f31cff6762cc865d82f02b3f95675e6347c17f2f2f1d887	precis
0032_draft_relations	1970-01-01 00:00:00+00	ca787dbaa808dea9a994648724ad214f4af841aff994aadbf1b323b054d3b72b	precis
0033_news_kind	1970-01-01 00:00:00+00	4c8438ec9705f7f7cac7c814268b0ebb73634519393a3b4b6ad8b546ec7a8851	precis
0034_agentlog_kind	1970-01-01 00:00:00+00	c9a864e0c2edd9e1b25822a21df600e55e748c98c066939ba380bf62ac7036a8	precis
0035_chunk_blobs	1970-01-01 00:00:00+00	520a29b508388ef1b8faf735a86f6f988ba076c6b18ef2a73182461e8ac565c1	precis
0036_ref_handle	1970-01-01 00:00:00+00	82f73ef0b257897609586e1bc141fcccf64f20a56e7a3fb0a21538da966eff07	precis
0037_draft_list_chunks	1970-01-01 00:00:00+00	bc703b9fa670b3eaff500d23837933974fd3873ed7fa4aa8ad8c4e1fe9dce61a	precis
0037_plots_relation	1970-01-01 00:00:00+00	f483b0733b6fe5a905719a5a13cce94f089eb131c129857b4ee2c68ba2bfc65a	precis
0038_ref_last_viewed	1970-01-01 00:00:00+00	e825bd3630742956e32706a1b2394f0a4cc03efb95b07794eec5a12fbe183197	precis
0039_authored_relation	1970-01-01 00:00:00+00	4b27b09822b4a9a7d101f6d111f99af07fe2a55f240b182f084c9a9758beb53a	precis
0039_orcid_kind	1970-01-01 00:00:00+00	c13a025b1fcfabb3f54b1eaf7fc3cb6adb3c337746768cd18ecbd0fea8efaa76	precis
0040_cfp_requirement_relation	1970-01-01 00:00:00+00	bcc2b9d62e94846eed83fa009c040cfbc630c88eb23c17f7a75df63b66ed51ff	precis
0041_cad_kind	1970-01-01 00:00:00+00	31d0b9fd21c073c82dc08c1c5f9e70f1f6cf1d66bc77f9285501ae3c485b1f75	precis
0042_structure_kind	1970-01-01 00:00:00+00	95d244d9db9aa960b258df26a2f610a75cad13e53def3003f0cfcce178d07106	precis
0043_structure_runs	1970-01-01 00:00:00+00	41fe1db37b827708788c2f6fa0ca4ad1c639278a07182d91f0f6374528c094a7	precis
0044_struct_run_cache	1970-01-01 00:00:00+00	f8447a516c81b748b09e884ac1da42edcca73bd20c994d40730df02d40b8fbdd	precis
0045_chunk_claims	1970-01-01 00:00:00+00	444c31e7328160252af454115f1ac0a059bbb0b4bdf19adaed80fdba7426e24c	precis
0046_requested_by_relation	1970-01-01 00:00:00+00	9d5183bc1bbc9a4aa25a66ca8ff06bf7e5d43429d82995bf7298f52c8d151e48	precis
0047_pcb_kind	1970-01-01 00:00:00+00	126eb9befb55ddcdfb2de88ed0a8785cdeacc1831916f8dfe1c572308c91a3fd	precis
0048_folder_kind	1970-01-01 00:00:00+00	34d1b515bcf5d361af3a3f412bd988d4efb29cf98d69709437fe0f115ddacfd9	precis
0049_doi_lowercase_guard	1970-01-01 00:00:00+00	d0a1a086162ea990c093c36a8176d83fa1e83d519ec63756a61806dd35885ee5	precis
0050_memory_body_chunk	1970-01-01 00:00:00+00	61111fe86724dae3a97946e75156a0e964c2bb6beb62514d8944c034d55d85b3	precis
0051_summarize_hot_tier	1970-01-01 00:00:00+00	fb4441563c12f5d18682b6bcb039baf3608975d735ca601503bc6e1a3606b5b8	precis
0052_pdf_locations	1970-01-01 00:00:00+00	fc585f6393280721b19caf85397b8732cb62c4c2ecf245113f1685521c000c1f	precis
0053_edgar_kind	1970-01-01 00:00:00+00	34bbc93897948f90d3b51b7c8ca1869686ba3028579dc3b3a6f7c6a15bb26978	precis
0054_datasheet_of_relation	1970-01-01 00:00:00+00	3c5edca397ab516e151641f3d75bac2057e9f55f5f465299de2d8e876a21c67e	precis
0055_rename_structure_cursor_to_eye	1970-01-01 00:00:00+00	431e1c9e3cf5e5bdc4a0ce28e33306bf279fb71532de3b62000710b3de4eb71e	precis
0056_plan_kind	1970-01-01 00:00:00+00	52776a54259b8db9846afec9e831c05230f718d4b33c31348195c56d0740ebcd	precis
0057_figure_kind	1970-01-01 00:00:00+00	d25115cbe36134cccb5d59ac792c30981eb9d31c64206cf86a93fd07aef4238e	precis
0058_figure_notes	1970-01-01 00:00:00+00	5d44bb4e89eb0a5d747176a4cbca5bcc3be54404b704d3d6cf087944a55a72be	precis
0059_secrets_vault	1970-01-01 00:00:00+00	9d5cf6ebc0d5a3c19d75d79edb76efd488dc769aac3c531c3839d9f5bc47a44d	precis
0060_anki_kind	1970-01-01 00:00:00+00	0d42adb5a1473b455311927e9acd2ffacc10a01473bdaa003c2e27f79f4e9884	precis
0061_llm_call_log	1970-01-01 00:00:00+00	acf16d820347647b3ad5d9b63762c09bb34c06bb732ced9b3541277c237860fc	precis
0062_paper_glossary_chunk	1970-01-01 00:00:00+00	05ec8ef91c2893d263b0f6a96ff4c870410daef38a6e6f762a41aa28299ed632	precis
0063_concept_kind	1970-01-01 00:00:00+00	e70ff0b904c318a7ea4a8f344ee819ee49c9c0d076d42cad63d907ad60b3c5e1	precis
0064_depicts_relation	1970-01-01 00:00:00+00	4ce3f6dc5e2108ef8a33226db8f144b3bd65bbb01e7167850f46fe4ba405348c	precis
0065_quest_kind	1970-01-01 00:00:00+00	01afe59eabefb3b7baf5cf8274bdc2429f9a13dd4e8a4c5c4c8fa83465f4f292	precis
0066_mermaid_kind	1970-01-01 00:00:00+00	59eda92ff12416323aeb43e7c7eddf3b5cc688d7f8c01925a036f2186ec40200	precis
0067_dossier_relation	1970-01-01 00:00:00+00	af0823fb66ca3ed2ee48df38ebb4749020042a4d6dffd0e3bebbfec1cb6876da	precis
0068_chunks_forbid_body_text_update	1970-01-01 00:00:00+00	daf1cb3271637f6e03f2eac9fc577c1c042fcaa834eb611578e38dcb850b2af0	precis
0069_markup_provider	1970-01-01 00:00:00+00	56dea86035cb96a51d9ec6b28605aaa760a1b070839e64e5b791c5f15677d817	precis
0070_app_settings	1970-01-01 00:00:00+00	10d2822715007a3037e334ed197f64af69c274ffd8a50d3babc65e7893fa301e	precis
0071_llm_kind	1970-01-01 00:00:00+00	fa05712971e852574a7ed04ef365ba7cf05cc8a6ba3879dfd1fe4f5696c115c6	precis
0072_service_config	1970-01-01 00:00:00+00	87f1b3ef87625eb6ce73433c33e66e9c75b48fc6c50b3bcf8353a38944647689	precis
0073_resource_slots	1970-01-01 00:00:00+00	794fee846225f2cd6ee75d9f35858dbfdbf305c9843b9d00eab508a40b6f7c18	precis
0074_scheduler_leases	1970-01-01 00:00:00+00	2260e58134459caae2901acdcd76a556c51dbec15b532596c69ac4a4ad5aaaae	precis
0075_email_account	1970-01-01 00:00:00+00	2322a63fb53c2a1e7f162826f3c53b59d946b0d3c23820535c77fe3b0c8744c2	precis
0076_email_scan	1970-01-01 00:00:00+00	2e91429a14c65e5b3bde1f97db77186b045fbe6a96fae2a4a3ca2fc7dae6bfac	precis
0077_llm_call_log_hash_idx	1970-01-01 00:00:00+00	4e87afe4a1336d64280d0062c37d4099bcea834fbe43f58b30d7b5886b9d2292	precis
0078_drop_dead_indexes	1970-01-01 00:00:00+00	e9a0d0d34b31829f59551df7f24ed1c7d06ebad14dbe10a80663bbace100c846	precis
0079_agent_ro_gripe_carveout	1970-01-01 00:00:00+00	2582cf33b2e961f3e6344fa699696331c9b1344db95d40d95c1c823f35d6be2a	precis
0080_argument_graph_relations	1970-01-01 00:00:00+00	cf0b6e78a911d14fb2e61b4787202073da6835f7cf4fc9d5e4d9b0528d82320e	precis
0081_websearch_full_query_title	1970-01-01 00:00:00+00	12eb60d8d4c768ebcde3ef214c3291e5cfd51dd0ebd128a0513b7f7ed34dd10e	precis
0082_citation_full_claim_title	1970-01-01 00:00:00+00	a1670003ba6f992410281f3d206e2c1d76e8c4bf0c863afb23714d2b2a2844ff	precis
0083_draft_claim_chunk_kind	1970-01-01 00:00:00+00	a679817a637981be66272a121cdc5fdccf82d220dbb461046eb7acb28ca92446	precis
0084_struct_runs_method_provenance	1970-01-01 00:00:00+00	be549148264af8b653ca3c6edbf7b64ca30cc4564b2f8f601b18c6c6dc212f8e	precis
0085_integration_disposition_relations	1970-01-01 00:00:00+00	e4e3ad978411396c2228ff61a79cfd33eee6befb59cb0fa4fdb7b9da8cb2cd5a	precis
0086_chunk_review	1970-01-01 00:00:00+00	4c2ae4edb2d85ea4ad40e8c20fb9c3f955a54994e9762388d967cca1b2ca7d41	precis
0087_struct_runs_forces_charges	1970-01-01 00:00:00+00	2b8de86af059811b5ca4ad7c4bd55b6b6fdf9b9e933888f48dc41de2c6f2b12a	precis
0088_draft_copy_of_relation	1970-01-01 00:00:00+00	8b7b512bd8e0f561336d331d6783d759b6f7bd25092a203a078baf31ae16d377	precis
0089_paper_of_relation	1970-01-01 00:00:00+00	5c3409e2b2cd90ec194118bef1b8591ae9522bee8abf26b3d3512bfd777addc5	precis
0090_llm_tier_floor_relabel	1970-01-01 00:00:00+00	84c70474f2b4e471fc037afee7689404f0f0670ec0a1144462af4d77ef2afb1e	precis
0091_service_config_concurrency	1970-01-01 00:00:00+00	966418dd00798fa03f06b8aa78ebda14838b0234e10e191726b75c221269c0aa	precis
0092_material_kind	1970-01-01 00:00:00+00	d5a1b863e8ef0fad27118b633097bcdbd4423e109e49d23a84bcb755ebc28a90	precis
0093_component_kind	1970-01-01 00:00:00+00	5ad28527810176797614f498316f3b733934252a0b25fbb0eb339869ff7f0258	precis
0094_taproot_evidence_relations	1970-01-01 00:00:00+00	66f1fea226b6542f9ee91d663de2522d3225bfe23ec271cd7c925d39c2a48e47	precis
0095_component_contains	1970-01-01 00:00:00+00	4122d3c7df2b52ecb56ab5414c98f7e1c743ede563eec9b3a1393e5a665b549c	precis
0096_pg_defensive_timeouts_and_embed_stats	1970-01-01 00:00:00+00	fce27345166a0fe277f588bb3312b66dcd1286e3fedb9e2eebf35cd5d92d0655	precis
0097_hot_table_autovacuum	1970-01-01 00:00:00+00	6db8f6e157649a9a0902bd51f06cc25d3fba8deb8bd30716e2e229cd8115fff0	precis
0098_capture_chunk_autovacuum	1970-01-01 00:00:00+00	293487a36e4ac4228c0e9ba0745dce01927a204b0300c384f7d829aa268bcec3	precis
0099_alert_meta_columns	1970-01-01 00:00:00+00	3bf0050e90c3c304f5e096df45fea987abfdb86c03f40c44228acfb4a87afd43	precis
0100_taproot_refines_relation	1970-01-01 00:00:00+00	c56d998defe3876e61d6453684e55e84156e760976092faf4f72d90c2809301e	precis
0101_taproot_claim_embeddings	1970-01-01 00:00:00+00	56a647e30729ada366c30c31bb89857a4ff0f3820f244f1f78605d4b61cd6c8d	precis
0102_todo_facet_normalize	1970-01-01 00:00:00+00	4ecf7d81a05574818335ab49c6ae96a951d099e8901fdfef26fbe125a62447a5	precis
0103_ref_events_ref_source_ts_idx	1970-01-01 00:00:00+00	bb777b4ec3c8078014f0c796eb5f9c6dda37e8a82804e2d2589071fbfefe7c8e	precis
0104_service_config_expires_at	1970-01-01 00:00:00+00	606caf65e5964eadb0f44df38954e439becacf5ec246ab6b68883133f418d13c	precis
0105_awaits_evidence_relation	1970-01-01 00:00:00+00	ba99037eb5d1d132a86f3b7278c1b3d4f8061e49b09cb91f79d7bb9dac3342ab	precis
0106_s2_neighbors	1970-01-01 00:00:00+00	cd625b9bd8211c91973a4ff79d591accc9dde9c9e3d79ed6be7decea162a8ac3	precis
0107_rename_litellm_transport_to_local	1970-01-01 00:00:00+00	618997daebddb632c393f17230264485442f57d8cff15475a8a87075e0055c8f	precis
0108_paper_bib_entries	1970-01-01 00:00:00+00	39514dfc24e111f9c4957c42205687470de379ba2980608c03fb63da50fa4799	precis
0109_chunk_citations	1970-01-01 00:00:00+00	2c412672edf7d255abe8f6de1dcfbd6fafd59c9334adaaef05a7de1fe5fbe07a	precis
0110_email_scan_attempt_lease	1970-01-01 00:00:00+00	ae6de3cd03bb9941fb37e18341786627dda9f6224c2aa7765f2e6e112b522899	precis
0111_vault_events_client_identity	1970-01-01 00:00:00+00	bdbb7d71b5056c78ae17f9884bf33e8b79f36470f3055317a58f9650c3920140	precis
0112_llm_call_log_placement	1970-01-01 00:00:00+00	099fc37f5f9fa09d028715e2009ece97fa5c9c5345eb8f0264b4f7477efdf96c	precis
0113_host_heartbeat_log	1970-01-01 00:00:00+00	620209e20b925ba598d628763e3f055f2a7c69b40c1c101b2215c8fb1f267a3b	precis
0114_ref_tags_tag_created_idx	1970-01-01 00:00:00+00	a825b3eb847a6f047ee0b994c48173fe996d7ae019e90c82368e50ea646ff5dc	precis
0115_patent_family_relation	1970-01-01 00:00:00+00	5671a35dbbed4c5a9e234a73f5f5034ecd0785a8a94ea4edc9c7690d991771fd	precis
0116_ref_embeddings	1970-01-01 00:00:00+00	ad5470de386954c93fecb63a9323a5dc1895a69ca31cc193518a440806d2206f	precis
0117_cfp_kind_seed	1970-01-01 00:00:00+00	8698a190e4a4034391fdaf8ba4a6d979232a546a3732d88a1ce2d64229c71e2c	precis
0118_drop_dead_indexes	1970-01-01 00:00:00+00	bb85d0488c8ffb5f010dc385ad8b8339c5a4e88d86e59166d1460373d11c23ef	precis
0119_resource_slot_holds	1970-01-01 00:00:00+00	1bed291ef0dafe4dc6425ca05b843025ab33e381175fc58e547794fe066f2c85	precis
0120_run_log_chunk_kind	1970-01-01 00:00:00+00	cc6d8fa871fe8132d6d4ea5fca13a6bef8c1149ed2c117527bece1d7d918dad0	precis
0121_external_rate_limits	1970-01-01 00:00:00+00	826ee758446fbba6f7a016aec5a27698cb7f421997f02a32feaccf3282a672c3	precis
0122_llm_call_log_token_counts	1970-01-01 00:00:00+00	5108a21f8615b09bb58759cc391d45f18a9bca1655f926b61dc8af9e349f1aae	precis
0123_slot_hold_identity	1970-01-01 00:00:00+00	58fcc48aa27d4370937cb7e6079a53154359fb698f6a95f62bd22ce71bcf1d9a	precis
0124_worker_logs_handler_ts	1970-01-01 00:00:00+00	2bbba8c447ac7513981ad86f41c53d127a3f456e4c19f5fba233b34d0e5593e3	precis
0125_app_settings_updated_by	1970-01-01 00:00:00+00	5d4b1c523eecee83b1c5285bfc8db4802cf402281d1d34ece1fa40cc9caf6a93	precis
0126_taproot_conjunct_of_relation	1970-01-01 00:00:00+00	199c4e06c3eab66f4728094693f6758f38765a7fc8332d7612c242bdd165bf8e	precis
0127_seed_worker_actors	1970-01-01 00:00:00+00	8ce2abfd8c1ea301f0e3b7f298d8d1f5ac134f141a7696bc4ee9d9b2bf07d09f	precis
0128_nanopub_publish	1970-01-01 00:00:00+00	5ffe3301a92749da3548e6842e949028845e50f432a5cf36e8a31851f1c81a1b	precis
0129_nanopub_publish_gate	1970-01-01 00:00:00+00	f0bcf9c3d01cc5ebe5457f90df29153e2d6d481b2145568cf33a4ef56571f54e	precis
0130_nanopub_mirror	1970-01-01 00:00:00+00	c9f3e97bd4e0315f22aba967edab7e9a4925b74f98212eee39ee376d04484158	precis
0131_web_users	1970-01-01 00:00:00+00	10f0257972a3f88d5ac1aa36a1c44e19dbc452363a2d673a983dbbf3ab194846	precis
0132_doi_validation	1970-01-01 00:00:00+00	54781d6d96909365a88a2a9658e29b87b8da30f0430e0c276ba1eb4f203efc2d	precis
0133_tool_calls_ledger	1970-01-01 00:00:00+00	68f93ab566e5c466fc42c83b53ec05bf3444eccc2d77f7d47f36afe2a19409f8	precis
0134_web_users_orcid	1970-01-01 00:00:00+00	1ce20e79eda65b65334895c92673db80a4493452a89166d78f42dad6f4a7ce60	precis
0135_taproot_motivated_by_relation	1970-01-01 00:00:00+00	6f82c0e6fe40f7c33f73371bd9fdadde04e0a7ab2ecb5cddb4c1fa2787a25051	precis
0136_fk_covering_indexes	1970-01-01 00:00:00+00	8a96943ed5982a466860d28752f741fc923e4ad6cfcebd39a89c21ded9b3efc7	precis
0137_bump_salience_security_definer	1970-01-01 00:00:00+00	4abb7f39b70a46fc2f6fff461066213735a4de254497a31a5eb2dabae02e48cd	precis
0138_pcb_boards_routes	1970-01-01 00:00:00+00	3d20a0200f50b363abf981920e2485f2049eb038d972fcaaff4d8af84e97255b	precis
0139_part_footprint_raw	1970-01-01 00:00:00+00	1107942367ed70fe4aeb1515c0797c8fe1ca24abf45ca26731be030863992a71	precis
0140_part_footprint_escape	1970-01-01 00:00:00+00	56e3a729042ae924dce0462fdce63d7e445d7659efdc546eb2e60e4b2818f395	precis
0141_pcb_pin_swaps	1970-01-01 00:00:00+00	fb72ab0080355903b85e9c362ca2e860b7cec30afc97d2613a1d30dbd5d1611b	precis
0142_quest_tests_relation	1970-01-01 00:00:00+00	cfaa21204eddb5dbe2790f1729ff94eb98f4803875c438bdcd64a86a7c94ee97	precis
0143_email_scan_depth	1970-01-01 00:00:00+00	b453a1c1e621866b0a529c4804466615167a0a859de8f39e6f809a24deff2295	precis
0144_claim_embeddings_hub_ref_id	1970-01-01 00:00:00+00	8ac8f3d1fb9ccb78a49ad2822d5ac168f4be8f4bea93128ca17fed41c62b79c7	precis
0145_quest_fidelity_ladder_keys	1970-01-01 00:00:00+00	9ab30557b43c2b86da6a2b60932a3a6d5dd047f42a5654f55cbd96e6d57fb1d0	precis
0146_review_digest_tag	1970-01-01 00:00:00+00	a01f77bda2dd363cbb3c9553114c33c131310bbd6f0c4d3a6120e47a43f21177	precis
0147_sandbox_run_wall_seconds_nest	1970-01-01 00:00:00+00	74d9bad17c34eb0625a71048b04398625b31160bd5d1cc570e2f0a1f99b704ea	precis
0148_dispatch_worker_minter_rename	1970-01-01 00:00:00+00	26f578a8cfff00017dbb1ea48e2938492e7afd9051cce8044c0bd367cf8bfda8	precis
0149_refs_retired_at_rename	1970-01-01 00:00:00+00	21ab31fe965793d0b355254f3ca10bef92162e265440f1f9578ca0d6211b937c	precis
\.
