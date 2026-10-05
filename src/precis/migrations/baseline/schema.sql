-- migrations/baseline/schema.sql — generated baseline snapshot.
--
-- DO NOT EDIT BY HAND. Regenerate with `precis db dump-schema`
-- (or `scripts/bump`, which does it at every version bump).
--
-- Baked-in migration head: 0189_secret_hint_counts
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
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS public;

--
-- PostgreSQL database dump
--


-- Dumped from database version 17.10 (Debian 17.10-1.pgdg12+1)
-- Dumped by pg_dump version 17.10 (Debian 17.10-1.pgdg12+1)

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
-- Name: gripe_status_check(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.gripe_status_check() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_ref_id bigint;
    v_n      integer;
    v_bad    integer;
BEGIN
    IF TG_TABLE_NAME = 'refs' THEN
        v_ref_id := NEW.ref_id;
    ELSIF TG_OP = 'DELETE' THEN
        v_ref_id := OLD.ref_id;
    ELSE
        v_ref_id := NEW.ref_id;
    END IF;

    -- Only gripes that still exist (a cascade delete removes the ref
    -- before this deferred trigger runs).
    IF NOT EXISTS (SELECT 1 FROM refs WHERE ref_id = v_ref_id AND kind = 'gripe') THEN
        RETURN NULL;
    END IF;

    SELECT count(*),
           count(*) FILTER (WHERE t.value NOT IN
               ('open', 'triaged', 'ready_for_fix', 'in_review', 'done', 'wontfix'))
      INTO v_n, v_bad
      FROM ref_tags rt
      JOIN tags t ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
     WHERE rt.ref_id = v_ref_id;

    IF v_n <> 1 OR v_bad > 0 THEN
        RAISE EXCEPTION
            'gripe % must have exactly one STATUS tag from '
            '(open, triaged, ready_for_fix, in_review, done, wontfix); '
            'found % STATUS tag(s), % off-vocabulary', v_ref_id, v_n, v_bad
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;


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
-- Name: precis_body_sha(bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_body_sha(p_ref_id bigint) RETURNS text
    LANGUAGE sql STABLE
    AS $$
    SELECT md5(coalesce(string_agg(md5(c.text), ',' ORDER BY c.ord, c.chunk_id), ''))
      FROM chunks c
     WHERE c.ref_id = p_ref_id AND c.ord >= 0 AND c.retired_at IS NULL
$$;


--
-- Name: precis_chunk_review_mirror(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_chunk_review_mirror() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    BEGIN
        IF TG_OP = 'DELETE' THEN
            INSERT INTO reviews (target_kind, target_id, actor, content_sha, verdict, note)
            VALUES ('chunk', OLD.chunk_id, OLD.checker,
                    coalesce(OLD.approved_sha, '(none)'), 'rejected', '(retracted)');
        ELSE
            INSERT INTO reviews (target_kind, target_id, actor, content_sha, verdict, note, at)
            VALUES ('chunk', NEW.chunk_id, NEW.checker,
                    coalesce(NEW.approved_sha, '(none)'),
                    CASE WHEN NEW.verdict ILIKE 'approved%' THEN 'approved' ELSE 'rejected' END,
                    NEW.verdict, NEW.at);
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'reviews: chunk_review mirror not written: %', SQLERRM;
    END;
    RETURN NULL;
END
$$;


--
-- Name: precis_chunks_body_revision(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_chunks_body_revision() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_ids bigint[];
    v_kinds text[];
    v_id bigint;
    v_ref refs;
    v_gone jsonb;
    v_prev_body text;
BEGIN
    SELECT array_agg(slug) INTO v_kinds FROM kinds WHERE covered_meta IS NOT NULL;
    IF v_kinds IS NULL THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'DELETE' THEN
        SELECT array_agg(DISTINCT r.ref_id) INTO v_ids
          FROM old_rows o JOIN refs r ON r.ref_id = o.ref_id
         WHERE o.ord >= 0 AND o.retired_at IS NULL AND r.created_at < now()
           AND r.kind = ANY (v_kinds);
    ELSE
        SELECT array_agg(DISTINCT r.ref_id) INTO v_ids
          FROM new_rows n JOIN refs r ON r.ref_id = n.ref_id
         WHERE n.ord >= 0 AND r.created_at < now()
           AND r.kind = ANY (v_kinds);
    END IF;
    IF v_ids IS NULL THEN
        RETURN NULL;
    END IF;
    BEGIN
        FOREACH v_id IN ARRAY v_ids LOOP
            SELECT * INTO v_ref FROM refs WHERE ref_id = v_id;
            IF TG_OP = 'DELETE' THEN
                SELECT jsonb_agg(jsonb_build_object(
                           'chunk_id', o.chunk_id, 'ord', o.ord,
                           'chunk_kind', o.chunk_kind, 'text', o.text)
                           ORDER BY o.ord, o.chunk_id)
                  INTO v_gone
                  FROM old_rows o
                 WHERE o.ref_id = v_id AND o.ord >= 0 AND o.retired_at IS NULL;
                SELECT md5(coalesce(string_agg(md5(t.text), ',' ORDER BY t.ord, t.chunk_id), ''))
                  INTO v_prev_body
                  FROM (SELECT c.chunk_id, c.ord, c.text FROM chunks c
                         WHERE c.ref_id = v_id AND c.ord >= 0 AND c.retired_at IS NULL
                        UNION ALL
                        SELECT o.chunk_id, o.ord, o.text FROM old_rows o
                         WHERE o.ref_id = v_id AND o.ord >= 0 AND o.retired_at IS NULL) t;
                PERFORM precis_log_revision(
                    'ref', v_id, 'edited', precis_ref_sha_of(v_ref, v_prev_body),
                    to_jsonb(v_ref) || jsonb_build_object('chunks', v_gone));
            ELSE
                SELECT md5(coalesce(string_agg(md5(c.text), ',' ORDER BY c.ord, c.chunk_id), ''))
                  INTO v_prev_body
                  FROM chunks c
                 WHERE c.ref_id = v_id AND c.ord >= 0 AND c.retired_at IS NULL
                   AND NOT EXISTS (SELECT 1 FROM new_rows n WHERE n.chunk_id = c.chunk_id);
                PERFORM precis_log_revision(
                    'ref', v_id, 'edited', precis_ref_sha_of(v_ref, v_prev_body),
                    to_jsonb(v_ref));
            END IF;
        END LOOP;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'revisions: chunk % not logged: %', TG_OP, SQLERRM;
    END;
    RETURN NULL;
END
$$;


--
-- Name: precis_hub_refine_mirror(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_hub_refine_mirror() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    BEGIN
        INSERT INTO reviews (target_kind, target_id, actor, version, content_sha, verdict, at)
        VALUES ('ref', NEW.ref_id, 'hub-refine',
                coalesce(NEW.meta ->> 'last_refined_version', '0'),
                precis_ref_sha_of(NEW, precis_body_sha(NEW.ref_id)), 'approved',
                coalesce(precis_try_timestamptz(NEW.meta ->> 'last_refined_at'), now()));
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'reviews: hub_refine mirror for ref % not written: %',
            NEW.ref_id, SQLERRM;
    END;
    RETURN NULL;
END
$$;


--
-- Name: precis_kind_covered_meta(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_kind_covered_meta(p_kind text) RETURNS text[]
    LANGUAGE plpgsql STABLE
    AS $$
BEGIN
    RETURN (SELECT covered_meta FROM kinds WHERE slug = p_kind);
END
$$;


--
-- Name: precis_kinds_covered_del(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_kinds_covered_del() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM old_rows WHERE covered_meta IS NOT NULL) THEN
        PERFORM precis_kinds_refresh_guarded();
    END IF;
    RETURN NULL;
END
$$;


--
-- Name: precis_kinds_covered_ins(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_kinds_covered_ins() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM new_rows WHERE covered_meta IS NOT NULL) THEN
        PERFORM precis_kinds_refresh_guarded();
    END IF;
    RETURN NULL;
END
$$;


--
-- Name: precis_kinds_covered_upd(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_kinds_covered_upd() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM precis_kinds_refresh_guarded();
    RETURN NULL;
END
$$;


--
-- Name: precis_kinds_refresh_guarded(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_kinds_refresh_guarded() RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
    BEGIN
        PERFORM precis_revision_triggers_refresh();
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'revisions: trigger refresh after kinds change failed: %', SQLERRM;
        INSERT INTO revision_trigger_state (singleton, last_error, last_error_at)
        VALUES (true, SQLERRM, now())
        ON CONFLICT (singleton) DO UPDATE SET
            last_error = EXCLUDED.last_error, last_error_at = EXCLUDED.last_error_at;
    END;
END
$$;


--
-- Name: precis_legacy_value_insert(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_legacy_value_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_table  text := TG_ARGV[0];
    v_key    text;
    v_tax    bigint;
    v_id     bigint;
    v_subj   bigint;
    c        measure_unit_compat;
    v_canon  text;
    v_meta   jsonb := '{}'::jsonb;
    v_runit  text;
    f        numeric;
    o        numeric;
BEGIN
    IF v_table = 'material_properties' THEN
        v_key := to_jsonb(NEW) ->> 'property_id';
        v_subj := (to_jsonb(NEW) ->> 'material_ref_id')::bigint;
    ELSE
        v_key := to_jsonb(NEW) ->> 'spec_id';
        v_subj := (to_jsonb(NEW) ->> 'component_ref_id')::bigint;
    END IF;
    v_tax := precis_measure_taxon(v_table, v_key, TRUE);
    IF v_tax IS NULL THEN
        RAISE EXCEPTION '% % is not registered', v_table, v_key
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    SELECT * INTO c FROM measure_unit_compat
     WHERE legacy_table = v_table AND legacy_key = v_key;
    IF FOUND THEN
        SELECT meta ->> 'canonical_unit' INTO v_canon FROM refs WHERE ref_id = v_tax;
        IF v_canon IS DISTINCT FROM c.si_unit THEN
            RAISE EXCEPTION
                '% % is stored in % but its compat row converts to %: refusing to '
                'write a legacy number that could be off by %',
                v_table, v_key, v_canon, c.si_unit, c.factor
                USING ERRCODE = 'check_violation';
        END IF;
        f := c.factor;
        o := c.si_offset;
        v_runit := c.legacy_unit;
        v_meta := jsonb_build_object('conversion', jsonb_build_object(
                      'from', c.legacy_unit, 'to', c.si_unit));
    END IF;
    v_id := nextval(pg_get_serial_sequence('measures', 'id'));
    INSERT INTO measures
        (id, subject_ref_id, measurand_ref_id, value_num, value_low, value_high,
         value_text, value_bool, input_unit, conditions, maturity, method,
         source_ref_id, source_chunk, source_url, as_of, actor, notes,
         literal, value_form, run_key, reported_unit, meta)
    OVERRIDING SYSTEM VALUE
    VALUES
        (v_id, v_subj, v_tax, precis_measure_si_value(NEW.value_num, f, o),
         precis_measure_si_value(NEW.value_low, f, o),
         precis_measure_si_value(NEW.value_high, f, o),
         NEW.value_text, NEW.value_bool, NEW.input_unit,
         coalesce(NEW.conditions, '{}'::jsonb), coalesce(NEW.maturity, 'lab'),
         NEW.method, NEW.source_ref_id, NEW.source_chunk, NEW.source_url,
         NEW.as_of, coalesce(nullif(btrim(NEW.set_by), ''), 'legacy'), NEW.notes,
         precis_measure_literal(NEW.value_num, NEW.value_low, NEW.value_high,
                                NEW.value_text, NEW.value_bool),
         precis_measure_form(NEW.value_num, NEW.value_low, NEW.value_high,
                             NEW.value_text, NEW.value_bool),
         'legacy:' || v_id, v_runit, v_meta);
    NEW.id := v_id;
    NEW.created_at := now();
    RETURN NEW;
END
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

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
-- Name: precis_link_content(public.links); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_link_content(l public.links) RETURNS jsonb
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT jsonb_build_object(
        'src_ref_id', l.src_ref_id, 'src_chunk_id', l.src_chunk_id,
        'dst_ref_id', l.dst_ref_id, 'dst_chunk_id', l.dst_chunk_id,
        'relation', l.relation,
        'meta', precis_pick_keys(coalesce(l.meta, '{}'::jsonb),
                                 precis_link_covered_meta()))
$$;


--
-- Name: precis_link_covered_meta(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_link_covered_meta() RETURNS text[]
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT ARRAY['support', 'support_reason', 'caveats', 'source_handle',
                 'quote', 'note', 'ruling']
$$;


--
-- Name: precis_link_sha_of(public.links); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_link_sha_of(l public.links) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT left(md5(precis_link_content(l)::text), 16)
$$;


--
-- Name: precis_link_verified_mirror(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_link_verified_mirror() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_stamp text := btrim(NEW.meta ->> 'verified_by');
BEGIN
    IF coalesce(v_stamp, '') = '' THEN
        RETURN NULL;
    END IF;
    BEGIN
        INSERT INTO reviews
            (target_kind, target_id, actor, model, content_sha, verdict, note, at)
        VALUES ('link', NEW.link_id, precis_reviewer_actor(v_stamp),
                precis_reviewer_model(v_stamp), precis_link_sha_of(NEW), 'approved',
                'links.meta.verified_by=' || v_stamp
                    || coalesce(', verified_claim_sha=' || (NEW.meta ->> 'verified_claim_sha'), ''),
                coalesce(precis_try_timestamptz(NEW.meta ->> 'verified_at'), now()));
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'reviews: verified_by mirror for link % not written: %',
            NEW.link_id, SQLERRM;
    END;
    RETURN NULL;
END
$$;


--
-- Name: precis_links_revision(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_links_revision() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NOT (
           OLD.created_at < now()
           AND precis_link_content(OLD) IS DISTINCT FROM precis_link_content(NEW)) THEN
        RETURN NULL;
    END IF;
    BEGIN
        PERFORM precis_log_revision(
            'link', OLD.link_id,
            CASE WHEN TG_OP = 'DELETE' THEN 'deleted' ELSE 'edited' END,
            precis_link_sha_of(OLD), to_jsonb(OLD));
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'revisions: % of link % not logged: %',
            TG_OP, OLD.link_id, SQLERRM;
    END;
    RETURN NULL;
END
$$;


--
-- Name: precis_log_revision(text, bigint, text, text, jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_log_revision(p_kind text, p_id bigint, p_event text, p_prev_sha text, p_prev_state jsonb) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_event text := p_event;
BEGIN
    IF p_event IN ('edited', 'retired')
       AND precis_revision_setting('event') = 'merged-into' THEN
        v_event := 'merged-into';
    END IF;
    INSERT INTO revisions AS r
        (target_kind, target_id, event, actor, model, reason,
         prev_sha, prev_state)
    VALUES (p_kind, p_id, v_event,
            coalesce(precis_revision_setting('actor'), session_user::text),
            precis_revision_setting('model'),
            coalesce(precis_revision_setting('reason'), '(unrecorded)'),
            p_prev_sha, p_prev_state)
    ON CONFLICT (target_kind, target_id, xact) DO UPDATE SET
        event = CASE WHEN r.event = 'edited' THEN EXCLUDED.event ELSE r.event END,
        prev_state = CASE
            WHEN EXCLUDED.prev_state ? 'chunks' AND NOT r.prev_state ? 'chunks'
            THEN r.prev_state || jsonb_build_object('chunks', EXCLUDED.prev_state -> 'chunks')
            ELSE r.prev_state END;
END
$$;


--
-- Name: precis_measure_form(double precision, double precision, double precision, text, boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_measure_form(p_num double precision, p_low double precision, p_high double precision, p_text text, p_bool boolean) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT CASE
        WHEN p_text IS NOT NULL THEN 'categorical'
        WHEN p_bool IS NOT NULL THEN 'boolean'
        WHEN p_num IS NOT NULL THEN 'point'
        WHEN p_low IS NOT NULL AND p_high IS NOT NULL THEN 'interval'
        WHEN p_low IS NOT NULL THEN 'lower_bound'
        WHEN p_high IS NOT NULL THEN 'upper_bound'
        ELSE 'not_established' END
$$;


--
-- Name: precis_measure_legacy_value(double precision, numeric, numeric); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_measure_legacy_value(p_si double precision, p_factor numeric, p_offset numeric) RETURNS double precision
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT CASE
        WHEN p_si IS NULL OR p_factor IS NULL THEN p_si
        WHEN p_si IN ('Infinity'::float8, '-Infinity'::float8) THEN p_si
        ELSE ((((p_si::text::numeric - coalesce(p_offset, 0)) / p_factor)::double precision)
              ::numeric)::double precision
    END
$$;


--
-- Name: precis_measure_literal(double precision, double precision, double precision, text, boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_measure_literal(p_num double precision, p_low double precision, p_high double precision, p_text text, p_bool boolean) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT CASE
        WHEN p_text IS NOT NULL THEN p_text
        WHEN p_num IS NOT NULL THEN p_num::text
        WHEN p_low IS NOT NULL AND p_high IS NOT NULL THEN p_low::text || '–' || p_high::text
        WHEN p_low IS NOT NULL THEN '≥' || p_low::text
        WHEN p_high IS NOT NULL THEN '≤' || p_high::text
        WHEN p_bool IS NOT NULL THEN p_bool::text
        ELSE '(none)' END
$$;


--
-- Name: precis_measure_sha(bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_measure_sha(p_id bigint) RETURNS text
    LANGUAGE sql STABLE
    AS $$
    SELECT left(md5(concat_ws('|',
        precis_sha_part(m.subject_ref_id::text),
        precis_sha_part(m.measurand_ref_id::text),
        precis_sha_part(m.literal),
        precis_sha_part(m.reported_unit),
        precis_sha_float(m.value_num),
        precis_sha_float(m.value_low),
        precis_sha_float(m.value_high),
        precis_sha_part(m.value_text),
        precis_sha_part((CASE m.value_bool WHEN true THEN 't' WHEN false THEN 'f' END)::text),
        precis_sha_float(m.value_err),
        precis_sha_part(m.value_form),
        precis_sha_part(m.reference),
        precis_sha_part(m.tier),
        precis_sha_part(m.normalization),
        precis_sha_part(m.normalization_status),
        precis_sha_part(m.source_attribution),
        precis_sha_part(m.measurand_status),
        precis_sha_part(m.direction),
        precis_sha_part(m.role),
        precis_sha_part(m.run_key),
        precis_sha_part(m.subject),
        precis_sha_part(m.subject_group),
        precis_sha_part(m.experiment_ref_id::text),
        precis_sha_part(m.derived_from::text),
        precis_sha_part(m.primary_link_id::text),
        precis_sha_part(m.anchor_scheme),
        precis_sha_part(m.span::text),
        precis_sha_part(m.supersedes::text),
        precis_sha_part(m.source_ref_id::text),
        precis_sha_part(m.actor),
        precis_sha_part(m.model),
        precis_sha_part(m.input_unit),
        precis_sha_part(m.conditions::text),
        precis_sha_part(m.maturity),
        precis_sha_part(m.method),
        precis_sha_part(m.source_chunk),
        precis_sha_part(m.source_url),
        precis_sha_part(to_char(m.as_of, 'YYYY-MM-DD')),
        precis_sha_part(m.notes))), 16)
      FROM measures m WHERE m.id = p_id
$$;


--
-- Name: precis_measure_si_value(double precision, numeric, numeric); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_measure_si_value(p_legacy double precision, p_factor numeric, p_offset numeric) RETURNS double precision
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT CASE
        WHEN p_legacy IS NULL OR p_factor IS NULL THEN p_legacy
        WHEN p_legacy IN ('Infinity'::float8, '-Infinity'::float8) THEN p_legacy
        ELSE ((p_legacy::numeric * p_factor + coalesce(p_offset, 0))::double precision)
    END
$$;


--
-- Name: precis_measure_taxon(text, text, boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_measure_taxon(p_table text, p_key text, p_create boolean DEFAULT false) RETURNS bigint
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_src   jsonb := jsonb_build_object('table', p_table, 'key', p_key);
    v_id    bigint;
    v_name  text;
    v_unit  text;
    v_disp  text;
    v_vtype text;
    v_allow jsonb;
    v_std   text;
    v_hib   boolean;
    v_desc  text;
    v_def   text;
    v_dkind text;
    v_alias jsonb;
    v_meta  jsonb;
    v_start bigint;
    v_si    text;
BEGIN
    SELECT r.ref_id INTO v_id
      FROM refs r
     WHERE r.kind = 'taxon'
       AND r.meta -> 'legacy_source' ->> 'table' = p_table
       AND r.meta -> 'legacy_source' ->> 'key' = p_key
     ORDER BY (r.retired_at IS NOT NULL), r.ref_id
     LIMIT 1;
    IF v_id IS NOT NULL OR NOT p_create THEN
        RETURN v_id;
    END IF;

    -- one minter per (table, key); the loser of a race waits here, then
    -- re-checks and finds the winner's taxon
    PERFORM pg_advisory_xact_lock(
        hashtext('precis_measure_taxon:' || p_table || ':' || p_key));
    SELECT r.ref_id INTO v_id
      FROM refs r
     WHERE r.kind = 'taxon'
       AND r.meta -> 'legacy_source' ->> 'table' = p_table
       AND r.meta -> 'legacy_source' ->> 'key' = p_key
     ORDER BY (r.retired_at IS NOT NULL), r.ref_id
     LIMIT 1;
    IF v_id IS NOT NULL THEN
        RETURN v_id;
    END IF;

    IF p_table = 'material_properties' THEN
        SELECT name, canonical_unit, value_type, allowed_values, standard_ref,
               higher_is_better, description
          INTO v_name, v_unit, v_vtype, v_allow, v_std, v_hib, v_desc
          FROM material_properties WHERE prop_id = p_key;
    ELSIF p_table = 'component_specs' THEN
        SELECT name, canonical_unit, value_type, allowed_values, standard_ref,
               higher_is_better, description
          INTO v_name, v_unit, v_vtype, v_allow, v_std, v_hib, v_desc
          FROM component_specs WHERE spec_id = p_key;
    ELSIF p_table = 'rxn_properties' THEN
        SELECT name, canonical_unit, value_type, allowed_values, standard_ref,
               higher_is_better, description
          INTO v_name, v_unit, v_vtype, v_allow, v_std, v_hib, v_desc
          FROM rxn_properties WHERE prop_id = p_key;
    ELSE
        RETURN NULL;
    END IF;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    v_name := regexp_replace(btrim(v_name), '\s+', ' ', 'g');
    v_def := btrim(coalesce(v_desc, ''));
    IF v_def = '' THEN
        v_def := format(
            '%s: a %s term%s, carried over from the legacy %s registry, which recorded no fuller definition.',
            v_name, v_vtype,
            CASE WHEN v_unit IS NOT NULL THEN ' measured in ' || v_unit ELSE '' END,
            p_table);
    END IF;
    -- the legacy registry keeps its own unit; the taxon stores SI
    SELECT c.si_unit INTO v_si FROM measure_unit_compat c
     WHERE c.legacy_table = p_table AND c.legacy_key = p_key
       AND c.legacy_unit = v_unit;
    IF v_si IS NOT NULL THEN
        v_disp := v_unit;
        v_unit := v_si;
    END IF;
    v_dkind := CASE
        WHEN v_unit IS NOT NULL THEN NULL      -- unmapped; see taxon /unmapped
        WHEN v_vtype IN ('categorical', 'boolean', 'text') THEN 'categorical'
        ELSE 'dimensionless' END;
    v_alias := CASE WHEN lower(v_name) <> lower(p_key)
                    THEN jsonb_build_array(p_key) ELSE '[]'::jsonb END;
    v_meta := jsonb_strip_nulls(jsonb_build_object(
        'name', v_name,
        'norm_name', lower(v_name),
        'slug', trim(both '-' from regexp_replace(lower(v_name), '[^a-z0-9]+', '-', 'g')),
        'definition', v_def,
        'aliases', v_alias,
        'status', 'proposed',
        'dimension_kind', v_dkind,
        'canonical_unit', v_unit,
        'display_unit', v_disp,
        'value_type', v_vtype,
        'allowed_values', v_allow,
        'standard_ref', v_std,
        'higher_is_better', v_hib,
        'legacy_source', v_src));

    INSERT INTO refs (kind, title, meta) VALUES ('taxon', v_name, v_meta)
    RETURNING ref_id INTO v_id;
    RAISE NOTICE 'precis_measure_taxon: minted taxon % for %.%', v_id, p_table, p_key;

    INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta)
    VALUES (v_id, -1, 'card_combined',
            v_name || ' — ' || v_def
                || CASE WHEN jsonb_array_length(v_alias) > 0
                        THEN ' (aka ' || p_key || ')' ELSE '' END,
            '{}'::jsonb);

    SELECT r.ref_id INTO v_start
      FROM refs r
     WHERE r.kind = 'taxon' AND r.meta ->> 'start' = 'true'
       AND r.meta ->> 'slug' = 'measurand'
     ORDER BY r.ref_id LIMIT 1;
    IF v_start IS NOT NULL THEN
        INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by)
        VALUES (v_id, v_start, 'specialises', 'system')
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN v_id;
END
$$;


--
-- Name: precis_measures_frozen(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_measures_frozen() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    k text;
    -- a first supersession: both columns set together, onto another row that
    -- states the same number (same subject, measurand and direction)
    v_sup_ok boolean :=
        OLD.superseded_by IS NULL AND OLD.superseded_at IS NULL
        AND NEW.superseded_by IS NOT NULL AND NEW.superseded_at IS NOT NULL
        AND NEW.superseded_by <> OLD.id
        AND EXISTS (SELECT 1 FROM measures t
                     WHERE t.id = NEW.superseded_by
                       AND t.subject_ref_id = OLD.subject_ref_id
                       AND t.subject IS NOT DISTINCT FROM OLD.subject
                       AND t.measurand_ref_id = OLD.measurand_ref_id
                       AND t.direction = OLD.direction
                       AND t.superseded_by IS NULL);
    -- the foreign keys' own ON DELETE SET NULL: only once the target is gone
    v_link_ok boolean :=
        OLD.primary_link_id IS NOT NULL AND NEW.primary_link_id IS NULL
        AND NOT EXISTS (SELECT 1 FROM links WHERE link_id = OLD.primary_link_id);
    v_src_ok boolean :=
        OLD.source_ref_id IS NOT NULL AND NEW.source_ref_id IS NULL
        AND NOT EXISTS (SELECT 1 FROM refs WHERE ref_id = OLD.source_ref_id);
BEGIN
    FOR k IN
        SELECT coalesce(n.key, o.key)
          FROM jsonb_each(to_jsonb(NEW)) n
          FULL JOIN jsonb_each(to_jsonb(OLD)) o ON o.key = n.key
         WHERE n.value IS DISTINCT FROM o.value
           AND coalesce(n.key, o.key) NOT IN ('trusted', 'extraction_status')
           AND NOT (coalesce(n.key, o.key) IN ('superseded_by', 'superseded_at')
                    AND v_sup_ok)
           AND NOT (coalesce(n.key, o.key) = 'primary_link_id' AND v_link_ok)
           AND NOT (coalesce(n.key, o.key) = 'source_ref_id' AND v_src_ok)
    LOOP
        RAISE EXCEPTION
            'measures are append-only: column % of measure % is frozen '
            '(only trusted, extraction_status -> human_checked, a first '
            'superseded_by + superseded_at onto a row with the same subject, '
            'measurand and direction, and a primary_link_id / source_ref_id '
            'whose target is gone may change; supersede with a new row)',
            k, OLD.id USING ERRCODE = 'check_violation';
    END LOOP;
    IF NEW.extraction_status IS DISTINCT FROM OLD.extraction_status
       AND NEW.extraction_status <> 'human_checked' THEN
        RAISE EXCEPTION
            'measures are append-only: extraction_status of measure % may only '
            'be set to human_checked (computed on write, never asserted)', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;


--
-- Name: precis_measures_no_delete(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_measures_no_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        IF coalesce(current_setting('precis.allow_measures_truncate', true), '') = 'on' THEN
            RETURN NULL;
        END IF;
        RAISE EXCEPTION 'measures are append-only: TRUNCATE is refused'
            USING ERRCODE = 'check_violation';
    END IF;
    RAISE EXCEPTION 'measures are append-only: measure % cannot be deleted '
        '(supersede it with a new row)', OLD.id
        USING ERRCODE = 'check_violation';
END
$$;


--
-- Name: precis_measures_unitless_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_measures_unitless_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_canon text;
    v_name  text;
BEGIN
    SELECT t.meta ->> 'canonical_unit', t.meta ->> 'name' INTO v_canon, v_name
      FROM refs t
      JOIN measure_unit_compat c
        ON c.legacy_table = t.meta -> 'legacy_source' ->> 'table'
       AND c.legacy_key = t.meta -> 'legacy_source' ->> 'key'
       AND c.si_unit = t.meta ->> 'canonical_unit'
     WHERE t.ref_id = NEW.measurand_ref_id;
    IF FOUND THEN
        RAISE EXCEPTION
            'no unit given for % on % (canonical %); state the unit',
            quote_literal(NEW.literal),
            quote_literal(coalesce(v_name, 'this measurand')),
            quote_literal(v_canon)
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;


--
-- Name: precis_pick_keys(jsonb, text[]); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_pick_keys(m jsonb, keys text[]) RETURNS jsonb
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT coalesce(jsonb_object_agg(k, m -> k), '{}'::jsonb)
      FROM unnest(keys) AS k
     WHERE m ? k
$$;


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
    owner_login text,
    CONSTRAINT refs_doi_status_check CHECK (((doi_status IS NULL) OR (doi_status = ANY (ARRAY['valid'::text, 'not_found'::text])))),
    CONSTRAINT refs_pdf_role_check CHECK (((pdf_role IS NULL) OR (pdf_role = ANY (ARRAY['main'::text, 'supplement'::text, 'appendix'::text, 'front_matter'::text, 'back_matter'::text])))),
    CONSTRAINT refs_prio_check CHECK (((prio IS NULL) OR ((prio >= 1) AND (prio <= 10)))),
    CONSTRAINT refs_retraction_status_check CHECK (((retraction_status IS NULL) OR (retraction_status = ANY (ARRAY['retracted'::text, 'corrected'::text, 'expression_of_concern'::text]))))
)
WITH (fillfactor='85');


--
-- Name: COLUMN refs.owner_login; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.refs.owner_login IS 'FK to web_users.login — the human this ref belongs to. NULL means unowned (every kind but the per-user ones, e.g. anki, default to this). ON DELETE SET NULL: removing the web user orphans the row rather than deleting it.';


--
-- Name: precis_ref_content(public.refs); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_ref_content(r public.refs) RETURNS jsonb
    LANGUAGE sql STABLE
    AS $$
    SELECT jsonb_build_object(
        'kind', r.kind, 'title', r.title, 'authors', r.authors,
        'year', r.year, 'parent_id', r.parent_id,
        'meta', precis_pick_keys(coalesce(r.meta, '{}'::jsonb),
                                 coalesce(precis_kind_covered_meta(r.kind),
                                          '{}'::text[])))
$$;


--
-- Name: precis_ref_revision_due(public.refs, public.refs); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_ref_revision_due(o public.refs, n public.refs) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
    SELECT o.created_at < now()
       AND (precis_kind_covered_meta(o.kind) IS NOT NULL
            OR precis_kind_covered_meta(n.kind) IS NOT NULL)
       AND (precis_ref_content(o) IS DISTINCT FROM precis_ref_content(n)
            OR (o.retired_at IS NULL) <> (n.retired_at IS NULL))
$$;


--
-- Name: precis_ref_sha_of(public.refs, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_ref_sha_of(r public.refs, p_body_sha text) RETURNS text
    LANGUAGE sql STABLE
    AS $$
    SELECT left(md5(precis_ref_content(r)::text || '|' || coalesce(p_body_sha, '')), 16)
$$;


--
-- Name: precis_refs_revision(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_refs_revision() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_event text;
BEGIN
    -- The cheap due-test runs outside the exception block: plpgsql opens a
    -- savepoint on entry to a block with an EXCEPTION clause, and most
    -- covered-kind updates (bookkeeping meta) log nothing.
    IF TG_OP = 'UPDATE' AND NOT precis_ref_revision_due(OLD, NEW) THEN
        RETURN NULL;
    END IF;
    BEGIN
        IF TG_OP = 'DELETE' THEN
            PERFORM precis_log_revision(
                'ref', OLD.ref_id, 'deleted',
                precis_ref_sha_of(OLD, precis_body_sha(OLD.ref_id)), to_jsonb(OLD));
        ELSE
            v_event := CASE
                WHEN OLD.retired_at IS NULL AND NEW.retired_at IS NOT NULL THEN 'retired'
                WHEN OLD.retired_at IS NOT NULL AND NEW.retired_at IS NULL THEN 'restored'
                ELSE 'edited' END;
            PERFORM precis_log_revision(
                'ref', NEW.ref_id, v_event,
                precis_ref_sha_of(OLD, precis_body_sha(NEW.ref_id)), to_jsonb(OLD));
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'revisions: % of ref % not logged: %',
            TG_OP, OLD.ref_id, SQLERRM;
    END;
    RETURN NULL;
END
$$;


--
-- Name: precis_reviewer_actor(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_reviewer_actor(p_stamp text) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $_$
    SELECT CASE
        WHEN p_stamp ILIKE '%reto ruling%' THEN 'reto'
        WHEN p_stamp ~ '^[^/ ]+/[^/ ]+$' THEN split_part(p_stamp, '/', 2)
        WHEN p_stamp LIKE 'agent:%' THEN substr(p_stamp, 7)
        ELSE p_stamp END
$_$;


--
-- Name: precis_reviewer_model(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_reviewer_model(p_stamp text) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $_$
    SELECT CASE WHEN p_stamp ~ '^[^/ ]+/[^/ ]+$' THEN split_part(p_stamp, '/', 1) END
$_$;


--
-- Name: precis_reviews_backfill(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_reviews_backfill() RETURNS integer
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_chunks integer;
    v_links integer;
BEGIN
    -- chunk_review rows (see precis_chunk_review_mirror for the verdict map).
    INSERT INTO reviews
        (target_kind, target_id, actor, model, version, content_sha, verdict, note, at)
    SELECT 'chunk', cr.chunk_id, cr.checker, NULL, '0',
           coalesce(cr.approved_sha, '(none)'),
           CASE WHEN cr.verdict ILIKE 'approved%' THEN 'approved' ELSE 'rejected' END,
           cr.verdict, cr.at
      FROM chunk_review cr
     WHERE NOT EXISTS (
            SELECT 1 FROM reviews v
             WHERE v.target_kind = 'chunk' AND v.target_id = cr.chunk_id
               AND v.actor = cr.checker AND v.at = cr.at);
    GET DIAGNOSTICS v_chunks = ROW_COUNT;

    -- links.meta.verified_by, at the link's sha now.
    INSERT INTO reviews
        (target_kind, target_id, actor, model, version, content_sha, verdict, note, at)
    SELECT 'link', l.link_id,
           precis_reviewer_actor(vb), precis_reviewer_model(vb),
           '0', precis_link_sha_of(l), 'approved',
           'backfill 0185 from links.meta.verified_by=' || vb
               || coalesce(', verified_claim_sha=' || (l.meta ->> 'verified_claim_sha'), ''),
           coalesce(precis_try_timestamptz(l.meta ->> 'verified_at'), l.created_at)
      FROM links l, LATERAL (SELECT btrim(l.meta ->> 'verified_by') AS vb) s
     WHERE coalesce(vb, '') <> ''
       AND NOT EXISTS (
            SELECT 1 FROM reviews v
             WHERE v.target_kind = 'link' AND v.target_id = l.link_id
               AND v.note LIKE 'backfill 0185 %');
    GET DIAGNOSTICS v_links = ROW_COUNT;
    RETURN v_chunks + v_links;
END
$$;


--
-- Name: precis_revision_setting(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_revision_setting(p_name text) RETURNS text
    LANGUAGE sql STABLE
    AS $$
    SELECT nullif(current_setting('precis.' || p_name, true), '')
$$;


--
-- Name: precis_revision_triggers_refresh(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_revision_triggers_refresh() RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET lock_timeout TO '3s'
    SET search_path TO 'public', 'pg_temp'
    AS $_$
DECLARE
    v_kinds text[];
    v_list text;
    -- Keys a worker stamps on links without changing their meaning. Only a
    -- prefilter: a key missing here costs one function call, never a wrong row.
    v_bookkeeping text[] := ARRAY['verified', 'verified_at', 'verified_by',
                                  'verified_claim_sha', 'candidate', 'elements'];
    v_state revision_trigger_state;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('precis_revision_triggers_refresh'));
    SELECT coalesce(array_agg(slug ORDER BY slug), '{}'::text[]) INTO v_kinds
      FROM kinds WHERE covered_meta IS NOT NULL;
    SELECT * INTO v_state FROM revision_trigger_state;
    IF FOUND
       AND v_state.covered_kinds IS NOT DISTINCT FROM v_kinds
       AND v_state.link_bookkeeping IS NOT DISTINCT FROM v_bookkeeping
       AND EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgname = 'links_revision_update'
                      AND tgrelid = 'public.links'::regclass)
       AND (cardinality(v_kinds) = 0
            OR EXISTS (SELECT 1 FROM pg_trigger
                        WHERE tgname = 'refs_revision_update'
                          AND tgrelid = 'public.refs'::regclass)) THEN
        -- triggers match the kinds: a stale last_error no longer applies
        IF v_state.last_error IS NOT NULL THEN
            UPDATE revision_trigger_state SET last_error = NULL, last_error_at = NULL;
        END IF;
        RETURN false;
    END IF;

    SELECT string_agg(format('%L', k), ', ' ORDER BY k) INTO v_list
      FROM unnest(v_kinds) AS k;

    DROP TRIGGER IF EXISTS refs_revision_update ON refs;
    DROP TRIGGER IF EXISTS refs_revision_kind ON refs;
    DROP TRIGGER IF EXISTS refs_revision_delete ON refs;
    IF v_list IS NOT NULL THEN
        EXECUTE format($t$
            CREATE TRIGGER refs_revision_update
                AFTER UPDATE ON refs FOR EACH ROW
                WHEN (OLD.kind IN (%s))
                EXECUTE FUNCTION precis_refs_revision()$t$, v_list);
        EXECUTE format($t$
            CREATE TRIGGER refs_revision_kind
                AFTER UPDATE OF kind ON refs FOR EACH ROW
                WHEN (NEW.kind IN (%s))
                EXECUTE FUNCTION precis_refs_revision()$t$, v_list);
        EXECUTE format($t$
            CREATE TRIGGER refs_revision_delete
                AFTER DELETE ON refs FOR EACH ROW
                WHEN (OLD.kind IN (%s) AND OLD.created_at < now())
                EXECUTE FUNCTION precis_refs_revision()$t$, v_list);
    END IF;

    DROP TRIGGER IF EXISTS links_revision_update ON links;
    DROP TRIGGER IF EXISTS links_revision_ends ON links;
    EXECUTE format($t$
        CREATE TRIGGER links_revision_update
            AFTER UPDATE ON links FOR EACH ROW
            WHEN ((OLD.meta - %1$L::text[]) IS DISTINCT FROM (NEW.meta - %1$L::text[]))
            EXECUTE FUNCTION precis_links_revision()$t$, v_bookkeeping);
    CREATE TRIGGER links_revision_ends
        AFTER UPDATE OF src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation
        ON links FOR EACH ROW
        EXECUTE FUNCTION precis_links_revision();

    INSERT INTO revision_trigger_state
        (singleton, covered_kinds, link_bookkeeping, refreshed_at, last_error, last_error_at)
    VALUES (true, v_kinds, v_bookkeeping, now(), NULL, NULL)
    ON CONFLICT (singleton) DO UPDATE SET
        covered_kinds = EXCLUDED.covered_kinds,
        link_bookkeeping = EXCLUDED.link_bookkeeping,
        refreshed_at = EXCLUDED.refreshed_at,
        last_error = NULL, last_error_at = NULL;
    RETURN true;
END
$_$;


--
-- Name: precis_revisions_seal(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_revisions_seal() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_event text;
    v_prev text;
    v_now text;
BEGIN
    BEGIN
        SELECT event, prev_sha INTO v_event, v_prev
          FROM revisions WHERE revision_id = NEW.revision_id;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        v_now := precis_target_sha(NEW.target_kind, NEW.target_id);
        IF v_event = 'edited' AND v_now IS NOT DISTINCT FROM v_prev THEN
            DELETE FROM revisions WHERE revision_id = NEW.revision_id;
        ELSE
            UPDATE revisions SET new_sha = v_now WHERE revision_id = NEW.revision_id;
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'revisions: seal of % not done: %', NEW.revision_id, SQLERRM;
    END;
    RETURN NULL;
END
$$;


--
-- Name: precis_sha_float(double precision); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_sha_float(p double precision) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT quote_nullable(encode(float8send(p), 'hex')) $$;


--
-- Name: precis_sha_part(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_sha_part(p text) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$ SELECT quote_nullable(p) $$;


--
-- Name: precis_target_sha(text, bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_target_sha(p_kind text, p_id bigint) RETURNS text
    LANGUAGE plpgsql STABLE
    AS $$
DECLARE
    s text;
BEGIN
    IF p_kind = 'chunk' THEN
        SELECT coalesce(c.content_sha, 'chunk:' || c.chunk_id) INTO s
          FROM chunks c WHERE c.chunk_id = p_id;
    ELSIF p_kind = 'ref' THEN
        SELECT precis_ref_sha_of(r, precis_body_sha(r.ref_id)) INTO s
          FROM refs r WHERE r.ref_id = p_id;
    ELSIF p_kind = 'link' THEN
        SELECT precis_link_sha_of(l) INTO s FROM links l WHERE l.link_id = p_id;
    ELSIF p_kind = 'measure' THEN
        s := precis_measure_sha(p_id);
    END IF;
    RETURN s;
END
$$;


--
-- Name: precis_taxon_unit_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_taxon_unit_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_canon boolean :=
        OLD.meta -> 'canonical_unit' IS DISTINCT FROM NEW.meta -> 'canonical_unit';
    v_dim boolean :=
        (OLD.meta -> 'dimension_kind' IS DISTINCT FROM NEW.meta -> 'dimension_kind'
         AND coalesce(OLD.meta -> 'dimension_kind', 'null'::jsonb) <> 'null'::jsonb)
        OR (OLD.meta -> 'si_vector' IS DISTINCT FROM NEW.meta -> 'si_vector'
            AND coalesce(OLD.meta -> 'si_vector', 'null'::jsonb) <> 'null'::jsonb);
BEGIN
    IF (v_canon OR v_dim) AND EXISTS (SELECT 1 FROM measures
                WHERE measurand_ref_id = OLD.ref_id AND superseded_by IS NULL) THEN
        IF v_canon AND NOT v_dim
           AND coalesce(current_setting('precis.allow_unit_rebase', true), '') = 'on'
           AND OLD.meta -> 'legacy_source' IS NOT DISTINCT FROM NEW.meta -> 'legacy_source'
           AND EXISTS (SELECT 1 FROM measure_unit_compat c
                        WHERE c.legacy_table = OLD.meta -> 'legacy_source' ->> 'table'
                          AND c.legacy_key = OLD.meta -> 'legacy_source' ->> 'key'
                          AND c.legacy_unit = OLD.meta ->> 'canonical_unit'
                          AND c.si_unit = NEW.meta ->> 'canonical_unit') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'taxon % has live measures stored in canonical_unit % (dimension_kind %, '
            'si_vector %); changing canonical_unit, or changing dimension_kind / '
            'si_vector from a value, would silently re-base them (supersede the '
            'measures first)',
            OLD.ref_id, OLD.meta ->> 'canonical_unit', OLD.meta ->> 'dimension_kind',
            OLD.meta ->> 'si_vector'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;


--
-- Name: precis_try_timestamptz(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.precis_try_timestamptz(p text) RETURNS timestamp with time zone
    LANGUAGE plpgsql IMMUTABLE
    AS $$
BEGIN
    RETURN p::timestamptz;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END
$$;


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
    SELECT (CASE
        WHEN v IS NULL OR length(v) = 0 THEN '(empty)'
        WHEN length(v) < 12 THEN repeat(chr(8226), 6) || ' (' || length(v) || ')'
        ELSE left(v, least(3, length(v) / 5)) || chr(8230)
             || right(v, least(2, length(v) / 5))
    END) || ' · ' || coalesce(length(v), 0) || ' chars · '
         || CASE WHEN v IS NULL OR v = '' THEN 0
                 ELSE cardinality(regexp_split_to_array(v, E'\\r\\n|\\r|\\n'))
            END || ' lines';
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
-- Name: chase_coverage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chase_coverage (
    hub_ref_id bigint NOT NULL,
    chunk_id bigint NOT NULL,
    embedder text NOT NULL,
    triggered_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE chase_coverage; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.chase_coverage IS 'Coverage ledger for the taproot chase trigger (plan transient-napping-parrot, Phase 1b). One row per (claim hub, triggering chunk) recorded by chase_trigger at due-mark time; hub_refine verifies exactly these chunks then deletes the row. A hub with any live row is due. See migration 0175 / workers/chase_trigger.py / workers/hub_refine.py.';


--
-- Name: checklist_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checklist_assignments (
    id bigint NOT NULL,
    target_ref_id bigint NOT NULL,
    checklist_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    retired_at timestamp with time zone
);


--
-- Name: TABLE checklist_assignments; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.checklist_assignments IS 'target_ref_id is explicitly assigned checklist_id. The assignment row is what makes silence honest: an assigned target with zero verdicts renders every item "not checked"; with no assignment row at all, get() renders "no checklist assigned", never empty-clean. v1 (slice 1) is explicit-only; kind-level default assignments (every pcb gets pcb-tapeout) arrive in slice 2.';


--
-- Name: checklist_assignments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.checklist_assignments ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.checklist_assignments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: checklist_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checklist_items (
    id bigint NOT NULL,
    checklist_id bigint NOT NULL,
    name text NOT NULL,
    rev integer NOT NULL,
    phase text,
    severity text DEFAULT 'advisory'::text NOT NULL,
    decidability text DEFAULT 'judgment'::text NOT NULL,
    prevents text,
    applies text,
    body text,
    origin text DEFAULT 'local'::text NOT NULL,
    target_ref_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    retired_at timestamp with time zone,
    CONSTRAINT checklist_items_decidability_check CHECK ((decidability = ANY (ARRAY['tool'::text, 'judgment'::text]))),
    CONSTRAINT checklist_items_origin_check CHECK ((origin = ANY (ARRAY['shipped'::text, 'local'::text]))),
    CONSTRAINT checklist_items_severity_check CHECK ((severity = ANY (ARRAY['blocking'::text, 'advisory'::text])))
);


--
-- Name: TABLE checklist_items; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.checklist_items IS 'Append-only per (checklist_id, name): current = the highest-rev live (retired_at IS NULL) row. An edit() or a deploy sync both insert rev+1 rather than rewriting; an item removed from a shipped file is retired, never deleted. origin is a property of the REV (not the name), so a local item promoted into the shipped file becomes the next rev in the same sequence — ordinary staleness, no retire-and-recreate. edit() REJECTS writes when the current live rev''s origin is ''shipped'' (git owns that content); local items always edit freely.';


--
-- Name: checklist_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.checklist_items ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.checklist_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: checklist_notes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checklist_notes (
    id bigint NOT NULL,
    target_ref_id bigint NOT NULL,
    checklist_id bigint NOT NULL,
    item_name text,
    name text NOT NULL,
    kind text NOT NULL,
    body text NOT NULL,
    re text,
    about jsonb DEFAULT '[]'::jsonb NOT NULL,
    origin text DEFAULT 'user'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    retired_at timestamp with time zone,
    CONSTRAINT checklist_notes_kind_check CHECK ((kind = ANY (ARRAY['question'::text, 'answer'::text, 'decision'::text])))
);


--
-- Name: TABLE checklist_notes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.checklist_notes IS 'Per-item (or per-run, when item_name IS NULL) argument threads — the se_notes shape verbatim (precis_se/0005_se_notes_freedom.sql is the model), lifted to src/precis/utils/notes.py so both kinds share one implementation. Name-keyed: re names the note this one answers/decides; about anchors sub-objects of the TARGET (e.g. pcb refdes/net names) or another item name — resolved at read time, dangling anchors reported not rejected, same as se. carries checklist_id (unlike se_notes, which has exactly one checklist per design) because a target can carry multiple assigned checklists that might define the same item name. Append-only: remove_note sets retired_at rather than deleting.';


--
-- Name: checklist_notes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.checklist_notes ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.checklist_notes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: checklist_verdicts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checklist_verdicts (
    id bigint NOT NULL,
    target_ref_id bigint NOT NULL,
    checklist_id bigint NOT NULL,
    item_name text NOT NULL,
    item_rev integer NOT NULL,
    verdict text NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    fingerprint text,
    checked_at timestamp with time zone DEFAULT now() NOT NULL,
    checked_by text,
    retired_at timestamp with time zone,
    CONSTRAINT checklist_verdicts_verdict_check CHECK ((verdict = ANY (ARRAY['pass'::text, 'fail'::text, 'n/a'::text, 'waived'::text])))
);


--
-- Name: TABLE checklist_verdicts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.checklist_verdicts IS 'The ledger is the primitive; "a run" is the current view over it — no run-as-row. Append-only: a re-check inserts a new row (never an UPDATE), the prior row stays queryable. item_rev pins what was judged: item_rev < the item''s current live rev is the staleness signal (item changed since check). fingerprint is caller-supplied and opaque in slice 1 (never computed by this migration''s code) — staleness-by-target-change activates once a kind starts supplying a real one. Status per (target, checklist, item_name) = the live row with the latest checked_at.';


--
-- Name: checklist_verdicts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.checklist_verdicts ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.checklist_verdicts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: checklists; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checklists (
    id bigint NOT NULL,
    name text NOT NULL,
    origin text DEFAULT 'local'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    retired_at timestamp with time zone,
    CONSTRAINT checklists_origin_check CHECK ((origin = ANY (ARRAY['shipped'::text, 'local'::text])))
);


--
-- Name: TABLE checklists; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.checklists IS 'Checklist definitions, addressed by name. origin=shipped checklists are created only by jobs/checklist_sync.py (from src/precis/data/checklists/*.yaml); origin=local checklists are created by put(kind=''checklist''). No hand-maintained version number here — git holds file history, checklist_items.rev holds item history.';


--
-- Name: checklists_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.checklists ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.checklists_id_seq
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
-- Name: measure_unit_compat; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.measure_unit_compat (
    legacy_table text NOT NULL,
    legacy_key text NOT NULL,
    legacy_unit text NOT NULL,
    si_unit text NOT NULL,
    factor numeric NOT NULL,
    si_offset numeric DEFAULT 0 NOT NULL,
    CONSTRAINT measure_unit_compat_check CHECK ((legacy_unit <> si_unit)),
    CONSTRAINT measure_unit_compat_factor_check CHECK ((factor > (0)::numeric)),
    CONSTRAINT measure_unit_compat_legacy_table_check CHECK ((legacy_table = ANY (ARRAY['material_properties'::text, 'component_specs'::text, 'rxn_properties'::text])))
);


--
-- Name: TABLE measure_unit_compat; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.measure_unit_compat IS 'Units the legacy taxa (meta.legacy_source = {table, key}, seeded by 0174) were kept in, and the SI unit they store now: si = legacy * factor + si_offset, computed in numeric (factor and offset are exact decimals) and cast to float8, so 25.4 mm is exactly 0.0254 m. Keyed by the taxon''s stable identifier, not its ref_id. Read by the material_values / component_spec_values views and their insert triggers, by precis_measure_taxon (a minted legacy taxon starts in SI) and by the store''s legacy-unit writers. Seed vocabulary: it rides in the baseline.';


--
-- Name: COLUMN measure_unit_compat.si_offset; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.measure_unit_compat.si_offset IS 'Additive part of the map (0 for every unit today; reserved for the first affine legacy unit). An uncertainty scales by factor only.';


--
-- Name: measures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.measures (
    id bigint NOT NULL,
    subject_ref_id bigint NOT NULL,
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
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    notes text,
    literal text NOT NULL,
    reported_unit text,
    value_form text,
    value_err double precision,
    reference text,
    tier text,
    trusted boolean,
    extraction_status text DEFAULT 'unverified'::text NOT NULL,
    normalization text,
    normalization_status text,
    source_attribution text,
    measurand_status text,
    measurand_ref_id bigint NOT NULL,
    direction text DEFAULT 'output'::text NOT NULL,
    role text,
    run_key text NOT NULL,
    subject text,
    subject_group text,
    experiment_ref_id bigint,
    derived_from bigint[],
    primary_link_id bigint,
    anchor_scheme text,
    span jsonb,
    supersedes bigint,
    superseded_by bigint,
    superseded_at timestamp with time zone,
    actor text NOT NULL,
    model text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT material_values_maturity_check CHECK ((maturity = ANY (ARRAY['commercial'::text, 'lab'::text, 'speculative'::text]))),
    CONSTRAINT measures_actor_check CHECK ((btrim(actor) <> ''::text)),
    CONSTRAINT measures_direction_check CHECK ((direction = ANY (ARRAY['input'::text, 'output'::text, 'covariate'::text]))),
    CONSTRAINT measures_extraction_status_check CHECK ((extraction_status = ANY (ARRAY['unverified'::text, 'anchor_matched'::text, 'anchor_mismatch'::text, 'human_checked'::text]))),
    CONSTRAINT measures_measurand_status_check CHECK ((measurand_status = ANY (ARRAY['explicit'::text, 'interpreted'::text, 'ambiguous'::text]))),
    CONSTRAINT measures_normalization_status_check CHECK ((normalization_status = ANY (ARRAY['explicit'::text, 'inferred'::text, 'unresolved'::text]))),
    CONSTRAINT measures_role_check CHECK ((role = ANY (ARRAY['context'::text, 'preparation'::text, 'model'::text]))),
    CONSTRAINT measures_source_attribution_check CHECK ((source_attribution = ANY (ARRAY['own_work'::text, 'cited_work'::text, 'not_established'::text]))),
    CONSTRAINT measures_tier_check CHECK ((tier = ANY (ARRAY['measured'::text, 'computed'::text, 'derived'::text, 'asserted'::text]))),
    CONSTRAINT measures_value_form_check CHECK ((value_form = ANY (ARRAY['point'::text, 'approximate_point'::text, 'upper_bound'::text, 'lower_bound'::text, 'interval'::text, 'categorical'::text, 'boolean'::text, 'not_established'::text])))
);


--
-- Name: TABLE measures; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.measures IS 'One sourced number per row (measures-substrate.md). Append-only: no DELETE or TRUNCATE, and a trigger refuses every UPDATE but the annotations; a change is a new row + supersedes. measurand_ref_id is a taxon ref and subject_ref_id any ref (handler-enforced; refs has no per-kind FK). Values are stored in the measurand taxon''s canonical_unit; literal + reported_unit keep what was printed. A run = one output row + its direction=input rows, tied by run_key. Reads of "current" numbers filter superseded_by IS NULL.';


--
-- Name: COLUMN measures.literal; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.measures.literal IS 'The exact reported string; the parsed value_* columns are the derived reading.';


--
-- Name: COLUMN measures.reference; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.measures.reference IS 'Reference state / convention (RHE, SHE, Ag/AgCl, ...): an input to the number, never folded into the unit.';


--
-- Name: COLUMN measures.tier; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.measures.tier IS 'measured | computed | derived | asserted; NULL = could not establish (never defaulted to asserted).';


--
-- Name: COLUMN measures.trusted; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.measures.trusted IS 'Denormalised read of findings-derived trust; NULL = unassessed. Annotation, not content.';


--
-- Name: COLUMN measures.extraction_status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.measures.extraction_status IS 'Computed on write: does the literal occur in the anchored span? human_checked is the one value an UPDATE may set.';


--
-- Name: COLUMN measures.normalization; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.measures.normalization IS 'Normalisation basis (per catalyst mass, per geometric area, per ECSA ...); not the same axis as reference.';


--
-- Name: COLUMN measures.run_key; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.measures.run_key IS 'Joins an output to exactly its own input rows. One key per claim x condition set; the compatibility views use legacy:<id>.';


--
-- Name: COLUMN measures.experiment_ref_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.measures.experiment_ref_id IS 'Reserved: the experiment kind fills it from run_key one to one when it ships.';


--
-- Name: COLUMN measures.primary_link_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.measures.primary_link_id IS 'The quantifies edge (paper chunk -> measurand taxon) that anchors this row; shared by every measure on that chunk and measurand. NULL on a measured row = anchor lost (the chunk or paper was deleted).';


--
-- Name: COLUMN measures.span; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.measures.span IS 'Where in the anchored chunk the number is printed (a string, or [chunk, start, end] raw offsets); anchor_scheme names the form. Further anchors: meta.extra_anchors, written at insert only.';


--
-- Name: COLUMN measures.actor; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.measures.actor IS 'Who wrote the row (same pair as reviews: actor + model); a legacy NULL set_by became ''legacy''.';


--
-- Name: component_spec_values; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.component_spec_values AS
 SELECT m.id,
    m.subject_ref_id AS component_ref_id,
    ((t.meta -> 'legacy_source'::text) ->> 'key'::text) AS spec_id,
    public.precis_measure_legacy_value(m.value_num, c.factor, c.si_offset) AS value_num,
    public.precis_measure_legacy_value(m.value_low, c.factor, c.si_offset) AS value_low,
    public.precis_measure_legacy_value(m.value_high, c.factor, c.si_offset) AS value_high,
    m.value_text,
    m.value_bool,
    m.input_unit,
    m.conditions,
    m.maturity,
    m.method,
    m.source_ref_id,
    m.source_chunk,
    m.source_url,
    m.as_of,
        CASE
            WHEN (m.actor = 'migration-0188'::text) THEN NULLIF((m.meta ->> 'legacy_actor'::text), 'legacy'::text)
            WHEN (m.actor = 'legacy'::text) THEN NULL::text
            ELSE m.actor
        END AS set_by,
    m.created_at,
    m.notes
   FROM ((public.measures m
     JOIN public.refs t ON ((t.ref_id = m.measurand_ref_id)))
     LEFT JOIN public.measure_unit_compat c ON (((c.legacy_table = 'component_specs'::text) AND (c.legacy_key = ((t.meta -> 'legacy_source'::text) ->> 'key'::text)) AND (c.si_unit = (t.meta ->> 'canonical_unit'::text)))))
  WHERE ((m.superseded_by IS NULL) AND (((t.meta -> 'legacy_source'::text) ->> 'table'::text) = 'component_specs'::text));


--
-- Name: VIEW component_spec_values; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.component_spec_values IS 'Compatibility view over measures (live rows whose measurand came from component_specs), in the spec''s LEGACY unit: values read back through measure_unit_compat. INSERT only, through an INSTEAD OF trigger that converts to SI and synthesises literal and run_key; there is no UPDATE or DELETE door.';


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
-- Name: design_block_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_block_state (
    ref_id bigint NOT NULL,
    block_uid bigint NOT NULL,
    state_name text NOT NULL,
    set_by text,
    set_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE design_block_state; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.design_block_state IS 'Which state each block is currently in, per (ref_id, block_uid) — a uid is preserved across branch copies, so keying on block_uid alone would let a branch steal its parent''s row. HYSTERESIS (addendum A9): a state-carrying block is the one place history is load-bearing — state is NOT a function of the parameter vector — so any cache or solver key over such a block MUST include this state. See precis.design.states.';


--
-- Name: design_block_uid_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.design_block_uid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: SEQUENCE design_block_uid_seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON SEQUENCE public.design_block_uid_seq IS 'Mint for stable block uids. A block gets ONE uid for its whole life; the renter''s persist layer carries it across retire-all/reinsert-all saves as column data. Never reuse, never reset.';


--
-- Name: design_branches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_branches (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    parent_ref_id bigint,
    parent_branch_id bigint,
    reason text NOT NULL,
    headline jsonb DEFAULT '{}'::jsonb NOT NULL,
    envelope_revision integer DEFAULT 0 NOT NULL,
    set_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT design_branches_reason_check CHECK ((btrim(reason) <> ''::text))
);


--
-- Name: design_branches_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.design_branches ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.design_branches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: design_checkpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_checkpoints (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    label text NOT NULL,
    envelope_revision integer DEFAULT 0 NOT NULL,
    headline jsonb DEFAULT '{}'::jsonb NOT NULL,
    payload jsonb NOT NULL,
    reason text,
    set_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE design_checkpoints; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.design_checkpoints IS 'checkpoint/restore (spec §1.5). payload is the owning plugin''s own serialised tree, opaque here — restore hands it straight back.';


--
-- Name: design_checkpoints_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.design_checkpoints ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.design_checkpoints_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: design_envelope_revisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_envelope_revisions (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    revision integer NOT NULL,
    block_uid bigint,
    reason text,
    set_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT design_envelope_revisions_revision_check CHECK ((revision > 0))
);


--
-- Name: design_envelope_revisions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.design_envelope_revisions ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.design_envelope_revisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: design_load_case_exemptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_load_case_exemptions (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    case_id text NOT NULL,
    reason text NOT NULL,
    set_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT design_load_case_exemptions_reason_check CHECK ((btrim(reason) <> ''::text))
);


--
-- Name: design_load_case_exemptions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.design_load_case_exemptions ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.design_load_case_exemptions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: design_load_cases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_load_cases (
    case_id text NOT NULL,
    name text NOT NULL,
    family text NOT NULL,
    standard boolean DEFAULT false NOT NULL,
    spec jsonb DEFAULT '{}'::jsonb NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT design_load_cases_family_check CHECK ((family = ANY (ARRAY['static'::text, 'shock'::text, 'vibration'::text, 'off_axis'::text, 'thermal'::text, 'fatigue'::text])))
);


--
-- Name: TABLE design_load_cases; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.design_load_cases IS 'The load-case library (spec §1.3). standard = applied by default to every design; exempting one needs a design_load_case_exemptions row carrying a reason.';


--
-- Name: design_revisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_revisions (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    rev integer NOT NULL,
    ops jsonb DEFAULT '[]'::jsonb NOT NULL,
    turn text,
    checkpoint_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT design_revisions_rev_check CHECK ((rev > 0))
);


--
-- Name: TABLE design_revisions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.design_revisions IS 'One row per saved version of a design: the ops that produced it and, for renters that do not version in rows (se), the design_checkpoints snapshot taken at that save. rev is the renter''s own version number (structure: refs.meta.version; se: 1 + prior row count). turn is the chat turn that produced the ops, NULL for a direct edit/put.';


--
-- Name: design_revisions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.design_revisions ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.design_revisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: design_scenario_link; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_scenario_link (
    ref_id bigint NOT NULL,
    scenario_id text NOT NULL,
    set_by text,
    set_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE design_scenario_link; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.design_scenario_link IS 'Which scenario governs a design. One row per design (PK on ref_id) — validate/DRC output records it so a verdict is never read out of context.';


--
-- Name: design_scenario_load_cases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_scenario_load_cases (
    scenario_id text NOT NULL,
    case_id text NOT NULL
);


--
-- Name: design_scenarios; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_scenarios (
    scenario_id text NOT NULL,
    name text NOT NULL,
    quantity bigint,
    objective_weights jsonb DEFAULT '{}'::jsonb NOT NULL,
    service_env_id text,
    status text DEFAULT 'proposed'::text NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT design_scenarios_status_check CHECK ((status = ANY (ARRAY['core'::text, 'proposed'::text])))
);


--
-- Name: TABLE design_scenarios; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.design_scenarios IS 'Top-level production context (spec §1.3): quantity, objective weights and the service environment, named once instead of hand-tuned per run. A design references exactly one, through design_scenario_link.';


--
-- Name: design_service_environments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_service_environments (
    env_id text NOT NULL,
    name text NOT NULL,
    lifetime_checks boolean DEFAULT true NOT NULL,
    expected_lifetime_s double precision,
    temp_min_k double precision,
    temp_max_k double precision,
    pressure_pa double precision,
    humidity_pct double precision,
    vibration_grms double precision,
    vibration_spectrum jsonb,
    cycle_count bigint,
    duty_cycle double precision,
    chemical_exposure text[] DEFAULT '{}'::text[] NOT NULL,
    status text DEFAULT 'proposed'::text NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT design_service_environments_status_check CHECK ((status = ANY (ARRAY['core'::text, 'proposed'::text])))
);


--
-- Name: TABLE design_service_environments; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.design_service_environments IS 'What a design must survive (spec §1.3). core = seeded with the scenario presets; proposed = minted by a caller.';


--
-- Name: design_situations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_situations (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    name text NOT NULL,
    descr text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE design_situations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.design_situations IS 'STUB (design-state-core.md item 1): a named swept-volume bundle — assembly, maintenance, shipping. The three-verdict rule table is build-order step 3 (situation-rule-tables.md).';


--
-- Name: design_situations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.design_situations ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.design_situations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: design_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_states (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    block_uid bigint NOT NULL,
    name text NOT NULL,
    envelope text,
    port_pose_overrides jsonb,
    descr text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    occupancy jsonb,
    pose jsonb
);


--
-- Name: TABLE design_states; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.design_states IS 'A block''s declared discrete states (blocktree slice 2). Per BLOCK, never per design: two independently switchable blocks sitting in different states is the photoswitch case, which a design-level pointer cannot represent.';


--
-- Name: design_states_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.design_states ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.design_states_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: design_transitions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.design_transitions (
    id bigint NOT NULL,
    ref_id bigint NOT NULL,
    block_uid bigint NOT NULL,
    from_state text NOT NULL,
    to_state text NOT NULL,
    driver_kind text NOT NULL,
    driver_ref text,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    requires jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT design_transitions_check CHECK ((from_state <> to_state)),
    CONSTRAINT design_transitions_driver_kind_check CHECK ((driver_kind = ANY (ARRAY['light'::text, 'reaction'::text, 'redox'::text, 'ph'::text, 'thermal'::text, 'mechanical'::text])))
);


--
-- Name: COLUMN design_transitions.requires; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.design_transitions.requires IS 'DECLARED target this transition is checked against (delta between ports, span, stimulus, bistable, cycles, ...) — distinct from params, the REALIZATION''s per-driver numbers (quantum yield, barrier height). Vetted at write time by precis_se.compose.parse_requires; read back by search(kind=''se'', compose=''<design>#<block>'') (port-pose-and-composition-search.md Decision 3). {} means no requirement. Declared intent only — no validate/drc/clearance pass reads it.';


--
-- Name: design_transitions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.design_transitions ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.design_transitions_id_seq
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
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    covered_meta text[]
);


--
-- Name: COLUMN kinds.covered_meta; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kinds.covered_meta IS 'Meta keys that are content for this kind (local-mesh-upkeep §2b). NULL = the kind keeps no revision history; ''{}'' = columns and body only. Read by precis_ref_content / the revisions triggers.';


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
    cache_creation_tokens integer,
    placement_routed text
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
-- Name: COLUMN llm_call_log.placement_routed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.llm_call_log.placement_routed IS 'local | cloud for the rung the router chose before any fallback (router._routed_placement). Compare with placement (the rung that ran) for the routed-vs-landed split. NULL = pre-0179 or a non-router writer.';


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
-- Name: material_values; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.material_values AS
 SELECT m.id,
    m.subject_ref_id AS material_ref_id,
    ((t.meta -> 'legacy_source'::text) ->> 'key'::text) AS property_id,
    public.precis_measure_legacy_value(m.value_num, c.factor, c.si_offset) AS value_num,
    public.precis_measure_legacy_value(m.value_low, c.factor, c.si_offset) AS value_low,
    public.precis_measure_legacy_value(m.value_high, c.factor, c.si_offset) AS value_high,
    m.value_text,
    m.value_bool,
    m.input_unit,
    m.conditions,
    m.maturity,
    m.method,
    m.source_ref_id,
    m.source_chunk,
    m.source_url,
    m.as_of,
        CASE
            WHEN (m.actor = 'migration-0188'::text) THEN NULLIF((m.meta ->> 'legacy_actor'::text), 'legacy'::text)
            WHEN (m.actor = 'legacy'::text) THEN NULL::text
            ELSE m.actor
        END AS set_by,
    m.created_at,
    m.notes
   FROM ((public.measures m
     JOIN public.refs t ON ((t.ref_id = m.measurand_ref_id)))
     LEFT JOIN public.measure_unit_compat c ON (((c.legacy_table = 'material_properties'::text) AND (c.legacy_key = ((t.meta -> 'legacy_source'::text) ->> 'key'::text)) AND (c.si_unit = (t.meta ->> 'canonical_unit'::text)))))
  WHERE ((m.superseded_by IS NULL) AND (((t.meta -> 'legacy_source'::text) ->> 'table'::text) = 'material_properties'::text));


--
-- Name: VIEW material_values; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.material_values IS 'Compatibility view over measures (live rows whose measurand came from material_properties), in the property''s LEGACY unit: values read back through measure_unit_compat. INSERT only, through an INSTEAD OF trigger that converts to SI and synthesises literal and run_key; there is no UPDATE or DELETE door.';


--
-- Name: measures_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.measures ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.measures_id_seq
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
    CONSTRAINT nanopub_publish_artifact_type_check CHECK ((artifact_type = ANY (ARRAY['claim'::text, 'composite'::text, 'hypothesis'::text]))),
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
-- Name: paper_authors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.paper_authors (
    ref_id bigint NOT NULL,
    "position" integer NOT NULL,
    given text DEFAULT ''::text NOT NULL,
    middle text DEFAULT ''::text NOT NULL,
    family text DEFAULT ''::text NOT NULL,
    name_raw text NOT NULL,
    orcid text,
    openalex_author_id text,
    person_ref_id bigint,
    source text NOT NULL,
    verified_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    full_name text GENERATED ALWAYS AS (btrim(regexp_replace(((((given || ' '::text) || middle) || ' '::text) || family), ' +'::text, ' '::text, 'g'::text))) STORED,
    CONSTRAINT paper_authors_orcid_format CHECK (((orcid IS NULL) OR (orcid ~ '^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]$'::text))),
    CONSTRAINT paper_authors_position_positive CHECK (("position" >= 1)),
    CONSTRAINT paper_authors_some_name CHECK (((given <> ''::text) OR (family <> ''::text) OR (name_raw <> ''::text))),
    CONSTRAINT paper_authors_source_check CHECK ((source = ANY (ARRAY['orcid'::text, 'crossref'::text, 'openalex'::text, 's2'::text, 'pdf'::text, 'legacy'::text, 'llm'::text, 'human'::text])))
);


--
-- Name: TABLE paper_authors; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.paper_authors IS 'One row per (paper, byline position). Source of truth for a paper''s authors; refs.authors is the jsonb projection regenerated by store.set_paper_authors. source = tier that wrote the row; verified_at = ORCID cross-check or human edit.';


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
-- Name: pcb_fixed_copper; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_fixed_copper (
    fixed_id bigint NOT NULL,
    board_id bigint NOT NULL,
    ref_id bigint NOT NULL,
    generator_name text NOT NULL,
    generator text NOT NULL,
    generator_version text,
    ctype text NOT NULL,
    layer text NOT NULL,
    net_id bigint,
    geom jsonb NOT NULL,
    envelope jsonb DEFAULT '{}'::jsonb NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    retired_at timestamp with time zone,
    CONSTRAINT pcb_fixed_copper_ctype_chk CHECK ((ctype = ANY (ARRAY['track'::text, 'via'::text])))
);


--
-- Name: TABLE pcb_fixed_copper; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_fixed_copper IS 'AUTHORED copper (pcb-pre-place-route-blocks Slice 1), an INPUT parallel to pcb_planes -- never a second writer of pcb_copper, which stays exactly the DERIVED, wholesale-regenerated table 0138 already defines. A generator re-apply with changed params soft-deletes (retired_at) exactly its own rows, scoped by (ref_id, generator_name), and re-inserts fresh -- the same identity discipline pcb_generators already keys idempotency on. A realize run seeds pcb_copper from this table''s active rows as pre-existing fixed segments and routes only what remains.';


--
-- Name: pcb_fixed_copper_fixed_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pcb_fixed_copper ALTER COLUMN fixed_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pcb_fixed_copper_fixed_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pcb_generators; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_generators (
    ref_id bigint NOT NULL,
    name text NOT NULL,
    generator text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    params jsonb NOT NULL,
    refdes text NOT NULL,
    ledger jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE pcb_generators; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_generators IS 'pcb-ewod-multitile Slice 2 -- one computed-component generator call per (ref_id, name): generator TYPE (e.g. ewod_pad_array) + fully-defaulted, canonical params + the ONE refdes it emits, for the idempotent no-op/retire-and-reinsert decision precis.store._pcb_ops makes on each put(generators=[...]). ledger is the last expansion''s capability summary (precis.pcb.generators.GeneratorExpansion.ledger), cached for read-back -- always re-derivable from params alone since expansion is a pure function.';


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
-- Name: pcb_local_footprints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pcb_local_footprints (
    ref_id bigint NOT NULL,
    name text NOT NULL,
    pads jsonb NOT NULL,
    pin_map jsonb,
    courtyard jsonb,
    centroid jsonb,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE pcb_local_footprints; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pcb_local_footprints IS 'Design-local authored footprints (pcb-ewod-multitile Slice 1) -- named pad geometry for an instance with no LCSC part, keyed by (ref_id, name); joined via pcb_components.footprint. Same {pads, pin_map, courtyard, centroid} row shape as the global part_footprints cache, so padplace/realize/gerber read either source identically.';


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
    working_voltage_v double precision,
    edge_rate_v_per_ns double precision,
    impedance_ohm double precision,
    function_hint text,
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
-- Name: COLUMN pcb_nets.working_voltage_v; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pcb_nets.working_voltage_v IS 'Peak working voltage of this net, volts (§E-1). NULL = not annotated. Consumed PAIRWISE: required clearance between two nets includes capabilities.conductor_spacing_mm(|V_a - V_b|, ...) — a scalar per-net attribute cannot express the constraint, so this column is an input to a pair computation, never a per-net clearance.';


--
-- Name: COLUMN pcb_nets.edge_rate_v_per_ns; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pcb_nets.edge_rate_v_per_ns IS 'Datasheet-derived switching edge rate, V/ns — the aggressor half of objectives.NetAnnotation. NULL = quiescent/DC, not asserted an aggressor without evidence.';


--
-- Name: COLUMN pcb_nets.impedance_ohm; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pcb_nets.impedance_ohm IS 'Datasheet-derived driving-point impedance, ohms — the victim half of objectives.NetAnnotation. NULL = unknown, treated as high-Z (worst-case victim), never as zero.';


--
-- Name: COLUMN pcb_nets.function_hint; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pcb_nets.function_hint IS 'A precis.pcb.objectives._FUNCTION_DEFAULTS key (crystal, switcher_sw, adc_input, digital_logic, power_rail) naming what this net DOES, used to pick a fallback NetAnnotation when the explicit columns above are NULL. Unvalidated on purpose: an unknown hint degrades to the conservative unknown default rather than failing the write.';


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
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    domain_kinds text[],
    range_kinds text[],
    functional boolean DEFAULT false NOT NULL,
    transitive boolean DEFAULT false NOT NULL,
    acyclic boolean DEFAULT false NOT NULL
);


--
-- Name: COLUMN relations.domain_kinds; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.relations.domain_kinds IS 'Allowed refs.kind of the link source; NULL = unconstrained.';


--
-- Name: COLUMN relations.range_kinds; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.relations.range_kinds IS 'Allowed refs.kind of the link target; NULL = unconstrained.';


--
-- Name: COLUMN relations.functional; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.relations.functional IS 'At most one live source ref per target for this relation (read through the inverse slug too).';


--
-- Name: COLUMN relations.transitive; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.relations.transitive IS 'Declaration only: A->B and B->C imply A->C. Drives no inference in v1.';


--
-- Name: COLUMN relations.acyclic; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.relations.acyclic IS 'The relation (in either stored direction) may not close a cycle.';


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
-- Name: reviews; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reviews (
    review_id bigint NOT NULL,
    target_kind text NOT NULL,
    target_id bigint NOT NULL,
    actor text NOT NULL,
    model text,
    version text DEFAULT '0'::text NOT NULL,
    content_sha text NOT NULL,
    verdict text NOT NULL,
    note text,
    at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT reviews_actor_check CHECK ((btrim(actor) <> ''::text)),
    CONSTRAINT reviews_target_kind_check CHECK ((target_kind = ANY (ARRAY['chunk'::text, 'ref'::text, 'link'::text, 'measure'::text]))),
    CONSTRAINT reviews_verdict_check CHECK ((verdict = ANY (ARRAY['proposed'::text, 'approved'::text, 'rejected'::text])))
);


--
-- Name: TABLE reviews; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.reviews IS 'Review ledger (local-mesh-upkeep §2): one row per review of a chunk, ref or link. Current while content_sha = precis_target_sha(kind, id). model NULL = a human. No FK on target: reviews of a deleted link stay as audit.';


--
-- Name: reviews_review_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.reviews_review_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: reviews_review_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.reviews_review_id_seq OWNED BY public.reviews.review_id;


--
-- Name: revision_trigger_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.revision_trigger_state (
    singleton boolean DEFAULT true NOT NULL,
    covered_kinds text[],
    link_bookkeeping text[],
    refreshed_at timestamp with time zone DEFAULT now() NOT NULL,
    last_error text,
    last_error_at timestamp with time zone,
    CONSTRAINT revision_trigger_state_singleton_check CHECK (singleton)
);


--
-- Name: TABLE revision_trigger_state; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.revision_trigger_state IS 'One row: the kinds.covered_meta kind list and the link bookkeeping-key list the revision triggers were last built from. last_error/last_error_at are set when a refresh after a kinds change failed (stale WHEN lists); cleared by the next successful refresh. Migration-managed, not data.';


--
-- Name: revisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.revisions (
    revision_id bigint NOT NULL,
    target_kind text NOT NULL,
    target_id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    xact bigint DEFAULT txid_current() NOT NULL,
    event text NOT NULL,
    actor text NOT NULL,
    model text,
    reason text NOT NULL,
    prev_sha text,
    new_sha text,
    prev_state jsonb NOT NULL,
    CONSTRAINT revisions_event_check CHECK ((event = ANY (ARRAY['edited'::text, 'retired'::text, 'restored'::text, 'deleted'::text, 'merged-into'::text]))),
    CONSTRAINT revisions_target_kind_check CHECK ((target_kind = ANY (ARRAY['ref'::text, 'link'::text])))
);


--
-- Name: TABLE revisions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.revisions IS 'Revision log (local-mesh-upkeep §2b): one row per transaction that changed a covered part of a ref or link. prev_state = the full prior row (+ replaced body chunks under "chunks"). Written by triggers; reason from SET LOCAL precis.reason, else (unrecorded).';


--
-- Name: revisions_revision_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.revisions_revision_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: revisions_revision_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.revisions_revision_id_seq OWNED BY public.revisions.revision_id;


--
-- Name: rxn_properties; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rxn_properties (
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
    CONSTRAINT rxn_properties_status_check CHECK ((status = ANY (ARRAY['core'::text, 'proposed'::text]))),
    CONSTRAINT rxn_properties_value_type_check CHECK ((value_type = ANY (ARRAY['quantity'::text, 'ratio'::text, 'categorical'::text, 'boolean'::text, 'text'::text])))
);


--
-- Name: TABLE rxn_properties; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.rxn_properties IS 'Typed, growable reaction-property registry (mirrors material_properties). core = curated starter set; proposed = minted at write time (must declare a canonical unit + dimension), never silently promoted.';


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
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    charge smallint DEFAULT 0 NOT NULL
);


--
-- Name: TABLE struct_atoms; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.struct_atoms IS 'ADR 0043 §4/§12: a design''s atoms — intent + current fractional position, including a declared formal charge (gr285775). Per-atom DERIVED outputs (force/partial charge) are run-scoped, not here. Never embedded.';


--
-- Name: COLUMN struct_atoms.charge; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.struct_atoms.charge IS 'Declared formal/net charge (intent) — e.g. a quaternary ammonium N+ or a carboxylate O-. Distinct from struct_runs.charges (DERIVED per-atom partial charge from a DFT/ML run). Feeds validate.py''s charge-aware valence budget (elements._CHARGED_VALENCE, gr285775).';


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
-- Name: reviews review_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reviews ALTER COLUMN review_id SET DEFAULT nextval('public.reviews_review_id_seq'::regclass);


--
-- Name: revisions revision_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.revisions ALTER COLUMN revision_id SET DEFAULT nextval('public.revisions_revision_id_seq'::regclass);


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
-- Name: chase_coverage chase_coverage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chase_coverage
    ADD CONSTRAINT chase_coverage_pkey PRIMARY KEY (hub_ref_id, chunk_id);


--
-- Name: checklist_assignments checklist_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_assignments
    ADD CONSTRAINT checklist_assignments_pkey PRIMARY KEY (id);


--
-- Name: checklist_items checklist_items_checklist_id_name_rev_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_items
    ADD CONSTRAINT checklist_items_checklist_id_name_rev_key UNIQUE (checklist_id, name, rev);


--
-- Name: checklist_items checklist_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_items
    ADD CONSTRAINT checklist_items_pkey PRIMARY KEY (id);


--
-- Name: checklist_notes checklist_notes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_notes
    ADD CONSTRAINT checklist_notes_pkey PRIMARY KEY (id);


--
-- Name: checklist_verdicts checklist_verdicts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_verdicts
    ADD CONSTRAINT checklist_verdicts_pkey PRIMARY KEY (id);


--
-- Name: checklists checklists_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklists
    ADD CONSTRAINT checklists_name_key UNIQUE (name);


--
-- Name: checklists checklists_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklists
    ADD CONSTRAINT checklists_pkey PRIMARY KEY (id);


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
-- Name: component_specs component_specs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.component_specs
    ADD CONSTRAINT component_specs_pkey PRIMARY KEY (spec_id);


--
-- Name: design_block_state design_block_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_block_state
    ADD CONSTRAINT design_block_state_pkey PRIMARY KEY (ref_id, block_uid);


--
-- Name: design_branches design_branches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_branches
    ADD CONSTRAINT design_branches_pkey PRIMARY KEY (id);


--
-- Name: design_branches design_branches_ref_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_branches
    ADD CONSTRAINT design_branches_ref_id_key UNIQUE (ref_id);


--
-- Name: design_checkpoints design_checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_checkpoints
    ADD CONSTRAINT design_checkpoints_pkey PRIMARY KEY (id);


--
-- Name: design_checkpoints design_checkpoints_ref_id_label_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_checkpoints
    ADD CONSTRAINT design_checkpoints_ref_id_label_key UNIQUE (ref_id, label);


--
-- Name: design_envelope_revisions design_envelope_revisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_envelope_revisions
    ADD CONSTRAINT design_envelope_revisions_pkey PRIMARY KEY (id);


--
-- Name: design_envelope_revisions design_envelope_revisions_ref_id_revision_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_envelope_revisions
    ADD CONSTRAINT design_envelope_revisions_ref_id_revision_key UNIQUE (ref_id, revision);


--
-- Name: design_load_case_exemptions design_load_case_exemptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_load_case_exemptions
    ADD CONSTRAINT design_load_case_exemptions_pkey PRIMARY KEY (id);


--
-- Name: design_load_case_exemptions design_load_case_exemptions_ref_id_case_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_load_case_exemptions
    ADD CONSTRAINT design_load_case_exemptions_ref_id_case_id_key UNIQUE (ref_id, case_id);


--
-- Name: design_load_cases design_load_cases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_load_cases
    ADD CONSTRAINT design_load_cases_pkey PRIMARY KEY (case_id);


--
-- Name: design_revisions design_revisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_revisions
    ADD CONSTRAINT design_revisions_pkey PRIMARY KEY (id);


--
-- Name: design_revisions design_revisions_ref_id_rev_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_revisions
    ADD CONSTRAINT design_revisions_ref_id_rev_key UNIQUE (ref_id, rev);


--
-- Name: design_scenario_link design_scenario_link_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_scenario_link
    ADD CONSTRAINT design_scenario_link_pkey PRIMARY KEY (ref_id);


--
-- Name: design_scenario_load_cases design_scenario_load_cases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_scenario_load_cases
    ADD CONSTRAINT design_scenario_load_cases_pkey PRIMARY KEY (scenario_id, case_id);


--
-- Name: design_scenarios design_scenarios_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_scenarios
    ADD CONSTRAINT design_scenarios_pkey PRIMARY KEY (scenario_id);


--
-- Name: design_service_environments design_service_environments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_service_environments
    ADD CONSTRAINT design_service_environments_pkey PRIMARY KEY (env_id);


--
-- Name: design_situations design_situations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_situations
    ADD CONSTRAINT design_situations_pkey PRIMARY KEY (id);


--
-- Name: design_situations design_situations_ref_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_situations
    ADD CONSTRAINT design_situations_ref_id_name_key UNIQUE (ref_id, name);


--
-- Name: design_states design_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_states
    ADD CONSTRAINT design_states_pkey PRIMARY KEY (id);


--
-- Name: design_states design_states_ref_id_block_uid_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_states
    ADD CONSTRAINT design_states_ref_id_block_uid_name_key UNIQUE (ref_id, block_uid, name);


--
-- Name: design_transitions design_transitions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_transitions
    ADD CONSTRAINT design_transitions_pkey PRIMARY KEY (id);


--
-- Name: design_transitions design_transitions_ref_id_block_uid_from_state_to_state_dri_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_transitions
    ADD CONSTRAINT design_transitions_ref_id_block_uid_from_state_to_state_dri_key UNIQUE (ref_id, block_uid, from_state, to_state, driver_kind);


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
-- Name: measure_unit_compat measure_unit_compat_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measure_unit_compat
    ADD CONSTRAINT measure_unit_compat_pkey PRIMARY KEY (legacy_table, legacy_key);


--
-- Name: measures measures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measures
    ADD CONSTRAINT measures_pkey PRIMARY KEY (id);


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
-- Name: paper_authors paper_authors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_authors
    ADD CONSTRAINT paper_authors_pkey PRIMARY KEY (ref_id, "position");


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
-- Name: pcb_fixed_copper pcb_fixed_copper_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_fixed_copper
    ADD CONSTRAINT pcb_fixed_copper_pkey PRIMARY KEY (fixed_id);


--
-- Name: pcb_generators pcb_generators_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_generators
    ADD CONSTRAINT pcb_generators_pkey PRIMARY KEY (ref_id, name);


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
-- Name: pcb_local_footprints pcb_local_footprints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_local_footprints
    ADD CONSTRAINT pcb_local_footprints_pkey PRIMARY KEY (ref_id, name);


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
-- Name: reviews reviews_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reviews
    ADD CONSTRAINT reviews_pkey PRIMARY KEY (review_id);


--
-- Name: revision_trigger_state revision_trigger_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.revision_trigger_state
    ADD CONSTRAINT revision_trigger_state_pkey PRIMARY KEY (singleton);


--
-- Name: revisions revisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.revisions
    ADD CONSTRAINT revisions_pkey PRIMARY KEY (revision_id);


--
-- Name: rxn_properties rxn_properties_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rxn_properties
    ADD CONSTRAINT rxn_properties_pkey PRIMARY KEY (prop_id);


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
-- Name: chase_coverage_chunk_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chase_coverage_chunk_id_idx ON public.chase_coverage USING btree (chunk_id);


--
-- Name: checklist_assignments_checklist_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checklist_assignments_checklist_id_idx ON public.checklist_assignments USING btree (checklist_id);


--
-- Name: checklist_assignments_live_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX checklist_assignments_live_idx ON public.checklist_assignments USING btree (target_ref_id, checklist_id) WHERE (retired_at IS NULL);


--
-- Name: checklist_assignments_target_ref_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checklist_assignments_target_ref_id_idx ON public.checklist_assignments USING btree (target_ref_id);


--
-- Name: checklist_items_live_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checklist_items_live_idx ON public.checklist_items USING btree (checklist_id, name) WHERE (retired_at IS NULL);


--
-- Name: checklist_items_target_ref_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checklist_items_target_ref_id_idx ON public.checklist_items USING btree (target_ref_id) WHERE (target_ref_id IS NOT NULL);


--
-- Name: checklist_notes_checklist_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checklist_notes_checklist_id_idx ON public.checklist_notes USING btree (checklist_id);


--
-- Name: checklist_notes_live_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checklist_notes_live_idx ON public.checklist_notes USING btree (target_ref_id, checklist_id) WHERE (retired_at IS NULL);


--
-- Name: checklist_notes_target_ref_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checklist_notes_target_ref_id_idx ON public.checklist_notes USING btree (target_ref_id);


--
-- Name: checklist_verdicts_checklist_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checklist_verdicts_checklist_id_idx ON public.checklist_verdicts USING btree (checklist_id);


--
-- Name: checklist_verdicts_latest_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checklist_verdicts_latest_idx ON public.checklist_verdicts USING btree (target_ref_id, checklist_id, item_name, checked_at DESC) WHERE (retired_at IS NULL);


--
-- Name: checklist_verdicts_target_ref_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checklist_verdicts_target_ref_id_idx ON public.checklist_verdicts USING btree (target_ref_id);


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
-- Name: component_specs_category_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX component_specs_category_idx ON public.component_specs USING btree (category_id);


--
-- Name: design_block_state_state_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX design_block_state_state_idx ON public.design_block_state USING btree (ref_id, block_uid, state_name);


--
-- Name: design_branches_parent_branch_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX design_branches_parent_branch_idx ON public.design_branches USING btree (parent_branch_id);


--
-- Name: design_branches_parent_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX design_branches_parent_ref_idx ON public.design_branches USING btree (parent_ref_id);


--
-- Name: design_load_case_exemptions_case_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX design_load_case_exemptions_case_idx ON public.design_load_case_exemptions USING btree (case_id);


--
-- Name: design_load_cases_standard_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX design_load_cases_standard_idx ON public.design_load_cases USING btree (case_id) WHERE standard;


--
-- Name: design_revisions_checkpoint_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX design_revisions_checkpoint_idx ON public.design_revisions USING btree (checkpoint_id);


--
-- Name: design_scenario_link_scenario_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX design_scenario_link_scenario_idx ON public.design_scenario_link USING btree (scenario_id);


--
-- Name: design_scenario_load_cases_case_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX design_scenario_load_cases_case_idx ON public.design_scenario_load_cases USING btree (case_id);


--
-- Name: design_scenarios_env_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX design_scenarios_env_idx ON public.design_scenarios USING btree (service_env_id);


--
-- Name: design_transitions_to_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX design_transitions_to_idx ON public.design_transitions USING btree (ref_id, block_uid, to_state);


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
-- Name: material_values_source_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX material_values_source_ref_idx ON public.measures USING btree (source_ref_id);


--
-- Name: measures_experiment_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX measures_experiment_idx ON public.measures USING btree (experiment_ref_id) WHERE (experiment_ref_id IS NOT NULL);


--
-- Name: measures_live_measurand_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX measures_live_measurand_idx ON public.measures USING btree (measurand_ref_id, value_num) WHERE (superseded_by IS NULL);


--
-- Name: measures_live_subject_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX measures_live_subject_idx ON public.measures USING btree (subject_ref_id, measurand_ref_id) WHERE (superseded_by IS NULL);


--
-- Name: measures_measurand_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX measures_measurand_idx ON public.measures USING btree (measurand_ref_id);


--
-- Name: measures_primary_link_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX measures_primary_link_idx ON public.measures USING btree (primary_link_id) WHERE (primary_link_id IS NOT NULL);


--
-- Name: measures_run_key_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX measures_run_key_idx ON public.measures USING btree (run_key);


--
-- Name: measures_subject_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX measures_subject_idx ON public.measures USING btree (subject_ref_id);


--
-- Name: measures_superseded_by_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX measures_superseded_by_idx ON public.measures USING btree (superseded_by) WHERE (superseded_by IS NOT NULL);


--
-- Name: measures_supersedes_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX measures_supersedes_idx ON public.measures USING btree (supersedes) WHERE (supersedes IS NOT NULL);


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
-- Name: paper_authors_family_lc_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX paper_authors_family_lc_idx ON public.paper_authors USING btree (lower(family));


--
-- Name: paper_authors_fullname_trgm_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX paper_authors_fullname_trgm_idx ON public.paper_authors USING gin (full_name public.gin_trgm_ops);


--
-- Name: paper_authors_openalex_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX paper_authors_openalex_idx ON public.paper_authors USING btree (openalex_author_id) WHERE (openalex_author_id IS NOT NULL);


--
-- Name: paper_authors_orcid_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX paper_authors_orcid_idx ON public.paper_authors USING btree (orcid) WHERE (orcid IS NOT NULL);


--
-- Name: paper_authors_person_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX paper_authors_person_idx ON public.paper_authors USING btree (person_ref_id) WHERE (person_ref_id IS NOT NULL);


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
-- Name: pcb_fixed_copper_board_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_fixed_copper_board_id_fk_idx ON public.pcb_fixed_copper USING btree (board_id);


--
-- Name: pcb_fixed_copper_board_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_fixed_copper_board_idx ON public.pcb_fixed_copper USING btree (board_id) WHERE (retired_at IS NULL);


--
-- Name: pcb_fixed_copper_net_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_fixed_copper_net_id_fk_idx ON public.pcb_fixed_copper USING btree (net_id);


--
-- Name: pcb_fixed_copper_ref_gen_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_fixed_copper_ref_gen_idx ON public.pcb_fixed_copper USING btree (ref_id, generator_name) WHERE (retired_at IS NULL);


--
-- Name: pcb_fixed_copper_ref_id_fk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pcb_fixed_copper_ref_id_fk_idx ON public.pcb_fixed_copper USING btree (ref_id);


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
-- Name: refs_kind_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_kind_owner_idx ON public.refs USING btree (kind, owner_login) WHERE (owner_login IS NOT NULL);


--
-- Name: refs_owner_login_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_owner_login_idx ON public.refs USING btree (owner_login) WHERE (owner_login IS NOT NULL);


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
-- Name: refs_rxn_class_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_rxn_class_idx ON public.refs USING btree (((meta ->> 'reaction_class'::text))) WHERE ((kind = 'rxn'::text) AND (meta ? 'reaction_class'::text));


--
-- Name: refs_rxn_uid_strict_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_rxn_uid_strict_idx ON public.refs USING btree (((meta ->> 'uid_strict'::text))) WHERE ((kind = 'rxn'::text) AND (meta ? 'uid_strict'::text));


--
-- Name: refs_rxn_uid_transform_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refs_rxn_uid_transform_idx ON public.refs USING btree (((meta ->> 'uid_transform'::text))) WHERE ((kind = 'rxn'::text) AND (meta ? 'uid_transform'::text));


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
-- Name: reviews_actor_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX reviews_actor_idx ON public.reviews USING btree (actor, verdict, at DESC);


--
-- Name: reviews_target_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX reviews_target_idx ON public.reviews USING btree (target_kind, target_id, at DESC);


--
-- Name: revisions_target_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX revisions_target_at_idx ON public.revisions USING btree (target_kind, target_id, at);


--
-- Name: revisions_target_xact_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX revisions_target_xact_uq ON public.revisions USING btree (target_kind, target_id, xact);


--
-- Name: revisions_unrecorded_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX revisions_unrecorded_idx ON public.revisions USING btree (at) WHERE (reason = '(unrecorded)'::text);


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
-- Name: chunk_review chunk_review_mirror; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER chunk_review_mirror AFTER INSERT OR DELETE OR UPDATE ON public.chunk_review FOR EACH ROW EXECUTE FUNCTION public.precis_chunk_review_mirror();


--
-- Name: chunks chunks_body_revision_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER chunks_body_revision_delete AFTER DELETE ON public.chunks REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION public.precis_chunks_body_revision();


--
-- Name: chunks chunks_body_revision_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER chunks_body_revision_insert AFTER INSERT ON public.chunks REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION public.precis_chunks_body_revision();


--
-- Name: chunks chunks_forbid_body_text_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER chunks_forbid_body_text_update BEFORE UPDATE ON public.chunks FOR EACH ROW WHEN (((new.text IS DISTINCT FROM old.text) AND (old.ord >= 0) AND (old.content_sha IS NULL))) EXECUTE FUNCTION public.chunks_forbid_body_text_update();


--
-- Name: component_spec_values component_spec_values_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER component_spec_values_insert INSTEAD OF INSERT ON public.component_spec_values FOR EACH ROW EXECUTE FUNCTION public.precis_legacy_value_insert('component_specs');


--
-- Name: ref_tags gripe_status_ref_tags; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER gripe_status_ref_tags AFTER INSERT OR DELETE OR UPDATE ON public.ref_tags DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.gripe_status_check();


--
-- Name: refs gripe_status_refs_ins; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER gripe_status_refs_ins AFTER INSERT ON public.refs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN ((new.kind = 'gripe'::text)) EXECUTE FUNCTION public.gripe_status_check();


--
-- Name: refs gripe_status_refs_kind; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER gripe_status_refs_kind AFTER UPDATE OF kind ON public.refs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN ((new.kind = 'gripe'::text)) EXECUTE FUNCTION public.gripe_status_check();


--
-- Name: kinds kinds_covered_del; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER kinds_covered_del AFTER DELETE ON public.kinds REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION public.precis_kinds_covered_del();


--
-- Name: kinds kinds_covered_ins; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER kinds_covered_ins AFTER INSERT ON public.kinds REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION public.precis_kinds_covered_ins();


--
-- Name: kinds kinds_covered_upd; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER kinds_covered_upd AFTER UPDATE OF covered_meta, slug ON public.kinds FOR EACH STATEMENT EXECUTE FUNCTION public.precis_kinds_covered_upd();


--
-- Name: links links_revision_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER links_revision_delete AFTER DELETE ON public.links FOR EACH ROW WHEN ((old.created_at < now())) EXECUTE FUNCTION public.precis_links_revision();


--
-- Name: links links_revision_ends; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER links_revision_ends AFTER UPDATE OF src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation ON public.links FOR EACH ROW EXECUTE FUNCTION public.precis_links_revision();


--
-- Name: links links_revision_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER links_revision_update AFTER UPDATE ON public.links FOR EACH ROW WHEN (((old.meta - '{verified,verified_at,verified_by,verified_claim_sha,candidate,elements}'::text[]) IS DISTINCT FROM (new.meta - '{verified,verified_at,verified_by,verified_claim_sha,candidate,elements}'::text[]))) EXECUTE FUNCTION public.precis_links_revision();


--
-- Name: links links_verified_mirror_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER links_verified_mirror_insert AFTER INSERT ON public.links FOR EACH ROW WHEN ((new.meta ? 'verified_by'::text)) EXECUTE FUNCTION public.precis_link_verified_mirror();


--
-- Name: links links_verified_mirror_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER links_verified_mirror_update AFTER UPDATE ON public.links FOR EACH ROW WHEN (((new.meta ? 'verified_by'::text) AND (((new.meta -> 'verified_by'::text) IS DISTINCT FROM (old.meta -> 'verified_by'::text)) OR ((new.meta -> 'verified_at'::text) IS DISTINCT FROM (old.meta -> 'verified_at'::text))))) EXECUTE FUNCTION public.precis_link_verified_mirror();


--
-- Name: material_values material_values_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER material_values_insert INSTEAD OF INSERT ON public.material_values FOR EACH ROW EXECUTE FUNCTION public.precis_legacy_value_insert('material_properties');


--
-- Name: measures measures_frozen; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER measures_frozen BEFORE UPDATE ON public.measures FOR EACH ROW EXECUTE FUNCTION public.precis_measures_frozen();


--
-- Name: measures measures_no_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER measures_no_delete BEFORE DELETE ON public.measures FOR EACH ROW EXECUTE FUNCTION public.precis_measures_no_delete();


--
-- Name: measures measures_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER measures_no_truncate BEFORE TRUNCATE ON public.measures FOR EACH STATEMENT EXECUTE FUNCTION public.precis_measures_no_delete();


--
-- Name: measures measures_unitless_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER measures_unitless_guard BEFORE INSERT ON public.measures FOR EACH ROW WHEN (((new.reported_unit IS NULL) AND ((new.value_num IS NOT NULL) OR (new.value_low IS NOT NULL) OR (new.value_high IS NOT NULL) OR (new.value_err IS NOT NULL)))) EXECUTE FUNCTION public.precis_measures_unitless_guard();


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
-- Name: refs refs_hub_refine_mirror; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER refs_hub_refine_mirror AFTER UPDATE ON public.refs FOR EACH ROW WHEN ((((new.meta -> 'last_refined_at'::text) IS NOT NULL) AND ((new.meta -> 'last_refined_at'::text) IS DISTINCT FROM (old.meta -> 'last_refined_at'::text)))) EXECUTE FUNCTION public.precis_hub_refine_mirror();


--
-- Name: refs refs_revision_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER refs_revision_delete AFTER DELETE ON public.refs FOR EACH ROW WHEN (((old.kind = ANY (ARRAY['citation'::text, 'concept'::text, 'finding'::text, 'memory'::text, 'taxon'::text])) AND (old.created_at < now()))) EXECUTE FUNCTION public.precis_refs_revision();


--
-- Name: refs refs_revision_kind; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER refs_revision_kind AFTER UPDATE OF kind ON public.refs FOR EACH ROW WHEN ((new.kind = ANY (ARRAY['citation'::text, 'concept'::text, 'finding'::text, 'memory'::text, 'taxon'::text]))) EXECUTE FUNCTION public.precis_refs_revision();


--
-- Name: refs refs_revision_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER refs_revision_update AFTER UPDATE ON public.refs FOR EACH ROW WHEN ((old.kind = ANY (ARRAY['citation'::text, 'concept'::text, 'finding'::text, 'memory'::text, 'taxon'::text]))) EXECUTE FUNCTION public.precis_refs_revision();


--
-- Name: refs refs_taxon_unit_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER refs_taxon_unit_guard BEFORE UPDATE OF meta ON public.refs FOR EACH ROW WHEN (((new.kind = 'taxon'::text) AND (((old.meta -> 'canonical_unit'::text) IS DISTINCT FROM (new.meta -> 'canonical_unit'::text)) OR ((old.meta -> 'dimension_kind'::text) IS DISTINCT FROM (new.meta -> 'dimension_kind'::text)) OR ((old.meta -> 'si_vector'::text) IS DISTINCT FROM (new.meta -> 'si_vector'::text))))) EXECUTE FUNCTION public.precis_taxon_unit_guard();


--
-- Name: revisions revisions_seal; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER revisions_seal AFTER INSERT ON public.revisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.precis_revisions_seal();


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
-- Name: chase_coverage chase_coverage_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chase_coverage
    ADD CONSTRAINT chase_coverage_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.chunks(chunk_id) ON DELETE CASCADE;


--
-- Name: chase_coverage chase_coverage_hub_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chase_coverage
    ADD CONSTRAINT chase_coverage_hub_ref_id_fkey FOREIGN KEY (hub_ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: checklist_assignments checklist_assignments_checklist_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_assignments
    ADD CONSTRAINT checklist_assignments_checklist_id_fkey FOREIGN KEY (checklist_id) REFERENCES public.checklists(id) ON DELETE CASCADE;


--
-- Name: checklist_assignments checklist_assignments_target_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_assignments
    ADD CONSTRAINT checklist_assignments_target_ref_id_fkey FOREIGN KEY (target_ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: checklist_items checklist_items_checklist_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_items
    ADD CONSTRAINT checklist_items_checklist_id_fkey FOREIGN KEY (checklist_id) REFERENCES public.checklists(id) ON DELETE CASCADE;


--
-- Name: checklist_items checklist_items_target_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_items
    ADD CONSTRAINT checklist_items_target_ref_id_fkey FOREIGN KEY (target_ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: checklist_notes checklist_notes_checklist_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_notes
    ADD CONSTRAINT checklist_notes_checklist_id_fkey FOREIGN KEY (checklist_id) REFERENCES public.checklists(id) ON DELETE CASCADE;


--
-- Name: checklist_notes checklist_notes_target_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_notes
    ADD CONSTRAINT checklist_notes_target_ref_id_fkey FOREIGN KEY (target_ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: checklist_verdicts checklist_verdicts_checklist_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_verdicts
    ADD CONSTRAINT checklist_verdicts_checklist_id_fkey FOREIGN KEY (checklist_id) REFERENCES public.checklists(id) ON DELETE CASCADE;


--
-- Name: checklist_verdicts checklist_verdicts_target_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_verdicts
    ADD CONSTRAINT checklist_verdicts_target_ref_id_fkey FOREIGN KEY (target_ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


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
-- Name: component_specs component_specs_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.component_specs
    ADD CONSTRAINT component_specs_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.component_categories(category_id);


--
-- Name: design_block_state design_block_state_ref_id_block_uid_state_name_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_block_state
    ADD CONSTRAINT design_block_state_ref_id_block_uid_state_name_fkey FOREIGN KEY (ref_id, block_uid, state_name) REFERENCES public.design_states(ref_id, block_uid, name) ON DELETE CASCADE;


--
-- Name: design_block_state design_block_state_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_block_state
    ADD CONSTRAINT design_block_state_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: design_branches design_branches_parent_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_branches
    ADD CONSTRAINT design_branches_parent_branch_id_fkey FOREIGN KEY (parent_branch_id) REFERENCES public.design_branches(id) ON DELETE SET NULL;


--
-- Name: design_branches design_branches_parent_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_branches
    ADD CONSTRAINT design_branches_parent_ref_id_fkey FOREIGN KEY (parent_ref_id) REFERENCES public.refs(ref_id) ON DELETE SET NULL;


--
-- Name: design_branches design_branches_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_branches
    ADD CONSTRAINT design_branches_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: design_checkpoints design_checkpoints_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_checkpoints
    ADD CONSTRAINT design_checkpoints_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: design_envelope_revisions design_envelope_revisions_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_envelope_revisions
    ADD CONSTRAINT design_envelope_revisions_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: design_load_case_exemptions design_load_case_exemptions_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_load_case_exemptions
    ADD CONSTRAINT design_load_case_exemptions_case_id_fkey FOREIGN KEY (case_id) REFERENCES public.design_load_cases(case_id) ON DELETE CASCADE;


--
-- Name: design_load_case_exemptions design_load_case_exemptions_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_load_case_exemptions
    ADD CONSTRAINT design_load_case_exemptions_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: design_revisions design_revisions_checkpoint_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_revisions
    ADD CONSTRAINT design_revisions_checkpoint_id_fkey FOREIGN KEY (checkpoint_id) REFERENCES public.design_checkpoints(id) ON DELETE SET NULL;


--
-- Name: design_revisions design_revisions_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_revisions
    ADD CONSTRAINT design_revisions_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: design_scenario_link design_scenario_link_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_scenario_link
    ADD CONSTRAINT design_scenario_link_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: design_scenario_link design_scenario_link_scenario_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_scenario_link
    ADD CONSTRAINT design_scenario_link_scenario_id_fkey FOREIGN KEY (scenario_id) REFERENCES public.design_scenarios(scenario_id);


--
-- Name: design_scenario_load_cases design_scenario_load_cases_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_scenario_load_cases
    ADD CONSTRAINT design_scenario_load_cases_case_id_fkey FOREIGN KEY (case_id) REFERENCES public.design_load_cases(case_id) ON DELETE CASCADE;


--
-- Name: design_scenario_load_cases design_scenario_load_cases_scenario_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_scenario_load_cases
    ADD CONSTRAINT design_scenario_load_cases_scenario_id_fkey FOREIGN KEY (scenario_id) REFERENCES public.design_scenarios(scenario_id) ON DELETE CASCADE;


--
-- Name: design_scenarios design_scenarios_service_env_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_scenarios
    ADD CONSTRAINT design_scenarios_service_env_id_fkey FOREIGN KEY (service_env_id) REFERENCES public.design_service_environments(env_id);


--
-- Name: design_situations design_situations_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_situations
    ADD CONSTRAINT design_situations_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: design_states design_states_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_states
    ADD CONSTRAINT design_states_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: design_transitions design_transitions_ref_id_block_uid_from_state_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_transitions
    ADD CONSTRAINT design_transitions_ref_id_block_uid_from_state_fkey FOREIGN KEY (ref_id, block_uid, from_state) REFERENCES public.design_states(ref_id, block_uid, name) ON DELETE CASCADE;


--
-- Name: design_transitions design_transitions_ref_id_block_uid_to_state_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_transitions
    ADD CONSTRAINT design_transitions_ref_id_block_uid_to_state_fkey FOREIGN KEY (ref_id, block_uid, to_state) REFERENCES public.design_states(ref_id, block_uid, name) ON DELETE CASCADE;


--
-- Name: design_transitions design_transitions_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.design_transitions
    ADD CONSTRAINT design_transitions_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


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
-- Name: measures material_values_source_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measures
    ADD CONSTRAINT material_values_source_ref_id_fkey FOREIGN KEY (source_ref_id) REFERENCES public.refs(ref_id) ON DELETE SET NULL;


--
-- Name: measures measures_experiment_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measures
    ADD CONSTRAINT measures_experiment_fk FOREIGN KEY (experiment_ref_id) REFERENCES public.refs(ref_id);


--
-- Name: measures measures_measurand_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measures
    ADD CONSTRAINT measures_measurand_fk FOREIGN KEY (measurand_ref_id) REFERENCES public.refs(ref_id);


--
-- Name: measures measures_primary_link_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measures
    ADD CONSTRAINT measures_primary_link_fk FOREIGN KEY (primary_link_id) REFERENCES public.links(link_id) ON DELETE SET NULL;


--
-- Name: measures measures_subject_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measures
    ADD CONSTRAINT measures_subject_fk FOREIGN KEY (subject_ref_id) REFERENCES public.refs(ref_id) ON DELETE RESTRICT;


--
-- Name: measures measures_superseded_by_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measures
    ADD CONSTRAINT measures_superseded_by_fk FOREIGN KEY (superseded_by) REFERENCES public.measures(id);


--
-- Name: measures measures_supersedes_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measures
    ADD CONSTRAINT measures_supersedes_fk FOREIGN KEY (supersedes) REFERENCES public.measures(id);


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
-- Name: paper_authors paper_authors_person_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_authors
    ADD CONSTRAINT paper_authors_person_ref_id_fkey FOREIGN KEY (person_ref_id) REFERENCES public.refs(ref_id) ON DELETE SET NULL;


--
-- Name: paper_authors paper_authors_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_authors
    ADD CONSTRAINT paper_authors_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


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
-- Name: pcb_fixed_copper pcb_fixed_copper_board_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_fixed_copper
    ADD CONSTRAINT pcb_fixed_copper_board_id_fkey FOREIGN KEY (board_id) REFERENCES public.pcb_boards(board_id) ON DELETE CASCADE;


--
-- Name: pcb_fixed_copper pcb_fixed_copper_net_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_fixed_copper
    ADD CONSTRAINT pcb_fixed_copper_net_id_fkey FOREIGN KEY (net_id) REFERENCES public.pcb_nets(net_id) ON DELETE CASCADE;


--
-- Name: pcb_fixed_copper pcb_fixed_copper_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_fixed_copper
    ADD CONSTRAINT pcb_fixed_copper_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


--
-- Name: pcb_generators pcb_generators_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_generators
    ADD CONSTRAINT pcb_generators_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


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
-- Name: pcb_local_footprints pcb_local_footprints_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pcb_local_footprints
    ADD CONSTRAINT pcb_local_footprints_ref_id_fkey FOREIGN KEY (ref_id) REFERENCES public.refs(ref_id) ON DELETE CASCADE;


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
-- Name: refs refs_owner_login_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refs
    ADD CONSTRAINT refs_owner_login_fkey FOREIGN KEY (owner_login) REFERENCES public.web_users(login) ON UPDATE CASCADE ON DELETE SET NULL;


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
-- Dumped by pg_dump version 17.10 (Debian 17.10-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET LOCAL search_path = public, pg_temp;
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
dream	Dreaming worker — mints speculative acquisitions from existing findings/claims for later review.	2026-10-05 07:16:44.305001+00
weave	Quest weave pass — automated quest-graph maintenance and stitching.	2026-10-05 07:16:44.305001+00
orcid	ORCID author-discovery stub minter — creates stub author records from ORCID lookups.	2026-10-05 07:16:44.305001+00
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
gripe_comment	f	Gripe comment / append-only timeline entry	\N	2026-10-05 07:16:44.174654+00
job_event	f	Job worker telemetry (forensics, not search)	\N	2026-10-05 07:16:44.174654+00
job_summary	f	Job completion summary (human-readable, searchable)	\N	2026-10-05 07:16:44.174654+00
pres_slide	f	Single slide of a deck (one chunk per slide). Distinct from ``paragraph`` so renderers can show slide numbers and so cross-kind search hits can be labelled as slides.	\N	2026-10-05 07:16:44.179153+00
cron_payload	f	Cron entry body — the natural-language payload that becomes the synthetic prompt to Asa when the cron fires. Searchable; embed + chunk_keywords workers index it normally.	\N	2026-10-05 07:16:44.181273+00
message_body	f	Outbound message body. The text that gets posted. Searchable so past sends can be retrieved with search(kind='message', q='...').	\N	2026-10-05 07:16:44.181273+00
flashcard_claim	f	Flashcard claim side	\N	2026-05-21 20:06:05.179981+00
flashcard_evidence	f	Flashcard evidence side	\N	2026-05-21 20:06:05.179981+00
job_result	f	Per-tick audit chunk written by the planner-coroutine when a plan_tick job finalises (verdict + summary + files). Read by the parent todo's next tick for context.	\N	2026-10-05 07:16:44.190474+00
tag_overflow	f	Long tag-value redirect chunk: when a put attempts to land a tag value longer than 80 chars in a redirectable namespace (ask-user / halt), the full value lands here and the tag becomes ``<ns>:see-chunk-<pos>``.	\N	2026-10-05 07:16:44.190474+00
aside	f	Draft aside / callout box (admonition; tcolorbox/mdframed on export).	\N	2026-10-05 07:16:44.202291+00
listing	f	Draft code listing — verbatim code payload, optional caption face.	\N	2026-10-05 07:16:44.202291+00
term	f	Glossary term — definition as face (text), {short, long, surface_forms} in meta; lives in a draft glossary subtree.	\N	2026-10-05 07:16:44.202291+00
ulist	f	Draft unordered-list container; its children are `item` chunks (renders to itemize on export).	\N	2026-10-05 07:16:44.208805+00
olist	f	Draft ordered-list container; its children are `item` chunks (renders to enumerate; meta may carry start/label style).	\N	2026-10-05 07:16:44.208805+00
item	f	Draft list item — a first-class child chunk under a `ulist`/`olist` (may itself contain nested lists / sub-paragraphs).	\N	2026-10-05 07:16:44.208805+00
edgar_section	f	One paragraph/section block of an SEC filing, labelled with its standard section via chunks.section_path + meta.item_code (e.g. Item 1A Risk Factors, 8-K Item 2.02). Distinct from ``paragraph`` so section-scoped search and the quarter-to-quarter diff can align the same section across consecutive filings.	\N	2026-10-05 07:16:44.234457+00
figure_node	f	A figure's SVG source document — the addressable source node (fn<id>). Raw markup: minted meta.no_index=true, never embedded.	\N	2026-10-05 07:16:44.237187+00
figure_vocab	f	A figure's shared vocabulary + drawing conventions — the negotiated ground truth ("green circles are foos"). Prose, embedded + searchable.	\N	2026-10-05 07:16:44.237187+00
figure_turn	f	One chat turn on a figure (user message + model reply) — the resumable session log. Prose, embedded + searchable.	\N	2026-10-05 07:16:44.237187+00
figure_notes	f	A figure's implementation notes — the model's private design log (element ids, structural scheme, conventions). Minted meta.no_index=true, never embedded; rendered behind the "Implementation notes" tab.	\N	2026-10-05 07:16:44.237859+00
card_glossary	t	Per-paper inferred reading glossary (clustered terms + one-line definitions); derived + embeddable, written by the paper_glossary worker at ord=-1000. See docs/design/reading-prep-loop.md.	\N	2026-10-05 07:16:44.243664+00
quest_log	f	Quest logbook entry — a WORM, dated, append-only ledger row (note / observation / hypothesis / result / decision / dead-end / milestone / reflection / cost) carrying entry_type + by + optional cost in meta. A milestone entry is a deed; a cost entry feeds the tote.	\N	2026-10-05 07:16:44.245616+00
mermaid_node	f	A mermaid diagram's source document — the addressable source node (mn<id>). Minted meta.no_index=true, never embedded.	\N	2026-10-05 07:16:44.246425+00
mermaid_vocab	f	A mermaid diagram's shared vocabulary + conventions — the negotiated ground truth. Prose, embedded + searchable.	\N	2026-10-05 07:16:44.246425+00
mermaid_notes	f	A mermaid diagram's private implementation notes (node ids, structure, conventions) — the model's design log. Minted no_index, not embedded.	\N	2026-10-05 07:16:44.246425+00
mermaid_turn	f	One chat turn on a mermaid diagram (user message + model reply) — the resumable session log. Prose, embedded + searchable.	\N	2026-10-05 07:16:44.246425+00
llm_review	f	LLM catalog review-log entry — a WORM, dated, append-only ledger row (published-benchmark / measured-eval / observed-telemetry / agent-review) carrying entry_type + by + provenance in meta. The ledger layer of the catalog; the tote rolls up llm_call_log alongside it (slice 3).	\N	2026-10-05 07:16:44.250162+00
claim	f	Draft claim statement — a discrete assertion under a Claims-style heading (patent claim drafting or a scientific claim list). Prose like paragraph; kept distinct so a renderer/reviewer can tell a claim from ordinary body text.	\N	2026-10-05 07:16:44.261343+00
run_log	f	Per-seed autocatpath run-log chunk — the tail of the compute child's captured stdout/stderr for one (model, seed) run. Forensics/provenance, not a search card (mirrors job_event / job_summary).	\N	2026-10-05 07:16:44.29946+00
step	f	One make-tree step (kind=make): an assembly/synthesis action whose conditions (fixture/torque; reagents/temperature) ride chunk meta; addressed mk<chunk_id>, aligned to blocks via made-by links.	\N	2026-10-05 07:16:44.34871+00
field	f	Sampled signed-distance grid of a cad field:<sha256> leaf. text = the one-line summary (shape, pitch, origin, source); meta.field = the payload header; the float32 samples live in chunk_blobs, content-addressed by sha256. Written by Store.put_field, never updated in place. See docs/backlog/cad-sdf-rounding-and-field-export.md.	\N	2026-10-05 07:16:44.381132+00
\.


--
-- Data for Name: component_categories; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.component_categories (category_id, name, status, description, created_at) FROM stdin;
fastener	Fastener	core	Bolts, screws, nuts, washers, rivets.	2026-10-05 07:16:44.271449+00
hose	Hose	core	Flexible fluid/gas conveyance.	2026-10-05 07:16:44.271449+00
pipe	Pipe	core	Rigid fluid/gas conveyance.	2026-10-05 07:16:44.271449+00
profile	Profile	core	Structural beams / extrusions.	2026-10-05 07:16:44.271449+00
electronic	Electronic	core	Discrete or module-level electronic components.	2026-10-05 07:16:44.271449+00
adhesive	Adhesive	core	Bonding compounds / tapes.	2026-10-05 07:16:44.271449+00
seal	Seal	core	Gaskets, o-rings, packings.	2026-10-05 07:16:44.271449+00
bearing	Bearing	core	Ball/roller/plain bearings, bushings.	2026-10-05 07:16:44.271449+00
fitting	Fitting	core	Pipe/hose connectors, adapters, unions.	2026-10-05 07:16:44.271449+00
laminate	Laminate	core	Layered composite sheet/panel material (measured specs only in v1 - no layer-structure model, see the proposal's deferrals).	2026-10-05 07:16:44.271449+00
\.


--
-- Data for Name: component_specs; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.component_specs (spec_id, name, canonical_unit, dimension, value_type, allowed_values, standard_ref, status, higher_is_better, description, category_id, created_at) FROM stdin;
mass	Mass	kg	mass	quantity	\N	\N	core	\N	Mass of one unit of the component.	\N	2026-10-05 07:16:44.271449+00
unit_cost	Unit cost	USD	currency	quantity	\N	\N	core	\N	Cost per unit (each/m/kg/... per uom=); pair with as_of and, for price breaks, conditions={"qty_break": 100}.	\N	2026-10-05 07:16:44.271449+00
length_overall	Overall length	m	length	quantity	\N	\N	core	\N	Overall length of one unit of the component.	\N	2026-10-05 07:16:44.271449+00
thread_size	Thread size	\N	categorical	categorical	["M3", "M4", "M5", "M6", "M8", "M10", "M12", "M16", "M20"]	\N	core	\N	Nominal metric thread designation.	fastener	2026-10-05 07:16:44.271449+00
thread_pitch	Thread pitch	mm	length	quantity	\N	\N	core	\N	Distance between adjacent thread crests.	fastener	2026-10-05 07:16:44.271449+00
length	Length	mm	length	quantity	\N	\N	core	\N	Fastener shank/overall length.	fastener	2026-10-05 07:16:44.271449+00
grade	Grade	\N	categorical	categorical	["4.8", "8.8", "10.9", "12.9", "A2", "A4"]	\N	core	\N	Strength/corrosion-resistance class marking.	fastener	2026-10-05 07:16:44.271449+00
drive_type	Drive type	\N	categorical	categorical	["hex", "socket", "phillips", "slotted", "torx", "allen"]	\N	core	\N	Tool interface for driving the fastener.	fastener	2026-10-05 07:16:44.271449+00
bore_diameter	Bore diameter	mm	length	quantity	\N	\N	core	\N	Inner (through-bore) diameter.	hose	2026-10-05 07:16:44.271449+00
max_working_pressure	Maximum working pressure	MPa	pressure/stress	quantity	\N	\N	core	\N	Rated continuous working pressure.	hose	2026-10-05 07:16:44.271449+00
min_bend_radius	Minimum bend radius	mm	length	quantity	\N	\N	core	\N	Smallest radius the hose may be bent to without kinking/damage.	hose	2026-10-05 07:16:44.271449+00
temperature_max	Maximum service temperature	K	temperature	quantity	\N	\N	core	\N	Upper continuous-use temperature. Absolute scale (Kelvin).	hose	2026-10-05 07:16:44.271449+00
bore_diameter_bearing	Bore diameter	mm	length	quantity	\N	\N	core	\N	Inner-ring bore diameter.	bearing	2026-10-05 07:16:44.271449+00
dynamic_load_rating	Dynamic load rating	N	force	quantity	\N	\N	core	\N	Basic dynamic load rating (rated fatigue life at constant load).	bearing	2026-10-05 07:16:44.271449+00
finish	Surface finish	\N	categorical	categorical	["plain", "zinc-plated", "black-oxide", "anodized"]	\N	proposed	\N	Surface treatment/coating.	fastener	2026-10-05 07:16:44.271449+00
is_reinforced	Is reinforced	\N	categorical	boolean	\N	\N	proposed	\N	Whether the hose carries a reinforcing braid/wire.	hose	2026-10-05 07:16:44.271449+00
outer_diameter	Outer diameter	mm	length	quantity	\N	\N	core	\N	Nominal outside diameter of the part's round envelope — screw shank, pipe/tube OD, washer OD, bearing outer race.	\N	2026-10-05 07:16:44.347129+00
inner_diameter	Inner diameter	mm	length	quantity	\N	\N	core	\N	Nominal through-hole diameter — washer ID, pipe bore, bearing bore. The geometric hole, distinct from the category-scoped functional bores (bore_diameter, bore_diameter_bearing).	\N	2026-10-05 07:16:44.347129+00
wall_thickness	Wall thickness	mm	length	quantity	\N	\N	core	\N	Wall thickness of a hollow section (tube, pipe, extruded profile).	\N	2026-10-05 07:16:44.347129+00
thickness	Thickness	mm	length	quantity	\N	\N	core	\N	Thickness of a flat part — sheet, plate, washer, shim. For stock sheet this is the discrete series value a cut part is realized at.	\N	2026-10-05 07:16:44.347129+00
width	Width	mm	length	quantity	\N	\N	core	\N	Width of the part's bounding extent across its section (rectangular profile, bearing width, strap).	\N	2026-10-05 07:16:44.347129+00
height	Height	mm	length	quantity	\N	\N	core	\N	Height of the part's bounding extent across its section, perpendicular to width.	\N	2026-10-05 07:16:44.347129+00
across_flats	Across flats	mm	length	quantity	\N	\N	core	\N	Wrench size — distance between opposing flats of an external hex (bolt head, nut). The spanner/socket the joint needs.	\N	2026-10-05 07:16:44.347129+00
head_diameter	Head diameter	mm	length	quantity	\N	\N	core	\N	Outside diameter of a fastener head (cap-screw head, washer face). What a counterbore must clear.	\N	2026-10-05 07:16:44.347129+00
head_height	Head height	mm	length	quantity	\N	\N	core	\N	Head height along the fastener axis — the protrusion above the clamped face, and the counterbore depth that would bury it.	\N	2026-10-05 07:16:44.347129+00
drive_size	Drive size	mm	length	quantity	\N	\N	core	\N	Size of the internal tool interface — hex-key across flats, Torx nominal. Pairs with drive_type; distinct from across_flats, which is the external hex.	\N	2026-10-05 07:16:44.347129+00
head_form	Head form	\N	categorical	categorical	["cap", "countersunk", "button", "pan", "hex", "flange", "none"]	\N	core	\N	Shape of a fastener head, which decides what the near member gets: countersunk => a cone, cap/pan/button => a counterbore or plain head clearance, none => a set screw with no head at all.	\N	2026-10-05 07:16:44.371677+00
point_type	Point type	\N	categorical	categorical	["machine", "tapping-c", "tapping-f", "thread-forming", "insert"]	\N	core	\N	How a fastener's far end engages: machine = a formed thread meeting a nut or a tapped hole; tapping-c = ISO 1478 sharp point, cuts/forms its own thread in a core hole; tapping-f = blunt/flat point; thread-forming = rolls a thread in thermoplastic without cutting; insert = not a screw, a threaded insert the screw meets.	\N	2026-10-05 07:16:44.371677+00
head_angle	Head angle	deg	angle	quantity	\N	\N	core	\N	Included angle of a countersunk head — 90 for the metric ISO families, 82 for the imperial ones. The angle the stamped countersink is cut at; absent on any other head form.	\N	2026-10-05 07:16:44.371677+00
drive_code	Drive code	\N	text	text	\N	\N	core	\N	The tool interface by its trade designation — T25, T30 for hexalobular/Torx. Pairs with drive_size (the millimetre extent) and drive_type (the family); this is the one you ask for in a shop.	\N	2026-10-05 07:16:44.371677+00
\.


--
-- Data for Name: design_load_cases; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.design_load_cases (case_id, name, family, standard, spec, description, created_at) FROM stdin;
std_shock	Handling shock	shock	t	{"pulse": "half_sine", "peak_g": 25.0, "duration_s": 0.011}	A dropped or knocked assembly. Half-sine pulse, applied along each principal axis in turn.	2026-10-05 07:16:44.362248+00
std_vibration	Transport vibration	vibration	t	{"grms": 1.5, "band_hz": [5.0, 500.0]}	Broadband transport vibration — the case that finds resonances a static check cannot see.	2026-10-05 07:16:44.362248+00
std_off_axis	Off-axis loading	off_axis	t	{"cone_deg": 30.0, "fraction_of_primary": 0.25}	A quarter of the primary load applied off the intended line of action. Catches designs that only work in one direction.	2026-10-05 07:16:44.362248+00
\.


--
-- Data for Name: design_service_environments; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.design_service_environments (env_id, name, lifetime_checks, expected_lifetime_s, temp_min_k, temp_max_k, pressure_pa, humidity_pct, vibration_grms, vibration_spectrum, cycle_count, duty_cycle, chemical_exposure, status, description, created_at) FROM stdin;
bench_week	Bench, one week	f	604800	288	303	\N	\N	\N	\N	1000	0.05	{}	core	A prototype that lives on a bench for a week. lifetime_checks FALSE drops corrosion, creep and fatigue entirely — they are not what kills this design.	2026-10-05 07:16:44.362248+00
indoor_5yr	Indoor service, five years	t	157680000	283	313	\N	\N	\N	\N	100000	0.2	{}	core	Ordinary indoor service. Fatigue and creep matter; corrosion is mild.	2026-10-05 07:16:44.362248+00
general_10yr	General service, ten years	t	315360000	243	333	\N	\N	\N	\N	1000000	0.4	{salt,uv}	core	Outdoor-capable general service: the lifetime families all run, and salt plus UV exposure are declared.	2026-10-05 07:16:44.362248+00
\.


--
-- Data for Name: design_scenarios; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.design_scenarios (scenario_id, name, quantity, objective_weights, service_env_id, status, description, created_at) FROM stdin;
prototype	Prototype	1	{"cost": 0.3, "mass": 0.1, "lead_time": 0.6}	bench_week	core	One-off. Getting it in hand beats getting it light or cheap; the lifetime families are off.	2026-10-05 07:16:44.362248+00
small_batch	Small batch	100	{"cost": 0.4, "mass": 0.3, "lead_time": 0.3}	indoor_5yr	core	Tens to hundreds. Per-unit cost starts to bite and the parts have to last; tooling still does not pay for itself.	2026-10-05 07:16:44.362248+00
mass_production	Mass production	100000	{"cost": 0.6, "mass": 0.3, "lead_time": 0.1}	general_10yr	core	Tooling amortises, so unit cost dominates and lead time barely registers. Full lifetime physics.	2026-10-05 07:16:44.362248+00
\.


--
-- Data for Name: embedders; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.embedders (name, dim, is_default, description, deprecated_at, created_at) FROM stdin;
bge-m3	1024	t	BAAI/bge-m3, dense; 1024-dim; multilingual	\N	2026-05-21 20:06:05.179981+00
\.


--
-- Data for Name: external_rate_limits; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.external_rate_limits (provider, capacity, refill_per_sec, tokens, last_refill, daily_cap, day_used, day_start) FROM stdin;
s2	2	1.0	2	2026-10-05 07:16:44.300029+00	\N	0	2026-10-05
openalex	10	8.0	10	2026-10-05 07:16:44.300029+00	100000	0	2026-10-05
unpaywall	5	5.0	5	2026-10-05 07:16:44.300029+00	100000	0	2026-10-05
arxiv	1	0.34	1	2026-10-05 07:16:44.300029+00	\N	0	2026-10-05
crossref	20	20.0	20	2026-10-05 07:16:44.300029+00	\N	0	2026-10-05
\.


--
-- Data for Name: kinds; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.kinds (slug, is_numeric, title, description, deprecated_at, created_at, covered_meta) FROM stdin;
paper	f	Paper	Research paper, addressed by cite_key	\N	2026-05-21 20:06:05.179981+00	\N
book	f	Book	Book or monograph	\N	2026-05-21 20:06:05.179981+00	\N
patent	f	Patent	Patent document	\N	2026-05-21 20:06:05.179981+00	\N
research_report	f	Research report	Research / industry report	\N	2026-05-21 20:06:05.179981+00	\N
oracle	f	Oracle	Oracle / authority node	\N	2026-05-21 20:06:05.179981+00	\N
skill	f	Skill	Agent skill document	\N	2026-05-21 20:06:05.179981+00	\N
tool	f	Tool	Tool spec or interface description	\N	2026-05-21 20:06:05.179981+00	\N
code	f	Code symbol	Function, class, module, or repo symbol	\N	2026-05-21 20:06:05.179981+00	\N
decision	f	Decision	ADR-style decision log entry	\N	2026-05-21 20:06:05.179981+00	\N
design	f	Design	Design document / plan	\N	2026-05-21 20:06:05.179981+00	\N
project	f	Project	Project descriptor (goals, status, …)	\N	2026-05-21 20:06:05.179981+00	\N
conv	f	Conversation	Conversation transcript	\N	2026-05-21 20:06:05.179981+00	\N
meeting	f	Meeting	Meeting notes / transcript	\N	2026-05-21 20:06:05.179981+00	\N
email	f	Email	Email message or thread	\N	2026-05-21 20:06:05.179981+00	\N
repo	f	Repo	Source-code repository	\N	2026-05-21 20:06:05.179981+00	\N
issue	f	Issue	Issue tracker item	\N	2026-05-21 20:06:05.179981+00	\N
todo	t	Todo	Task / action item	\N	2026-05-21 20:06:05.179981+00	\N
gripe	t	Gripe	Informal log entry	\N	2026-05-21 20:06:05.179981+00	\N
web	f	Web query	Cached web / research / think query	\N	2026-05-21 20:06:05.179981+00	\N
youtube	f	YouTube	Cached YouTube transcript	\N	2026-05-21 20:06:05.179981+00	\N
math	f	Math result	Cached Wolfram math result	\N	2026-05-21 20:06:05.179981+00	\N
markdown	f	Markdown file	Read / write .md / .markdown files under a configured root. Slug derived from path; lazy re-ingest on stale mtime; block slugs are content-stable. See src/precis/handlers/markdown.py.	\N	2026-06-04 19:55:50.290874+00	\N
plaintext	f	Plaintext file	Read / write .txt / .org / .rst files under a configured root. The shared file-kind base; markdown and tex are subclasses. See src/precis/handlers/plaintext.py.	\N	2026-06-04 19:55:50.290874+00	\N
tex	f	LaTeX file	Read / write .tex files under a configured root. Inherits the plaintext file-kind machinery; adds tex-aware block parsing + input-resolution. See src/precis/handlers/tex.py.	\N	2026-06-04 19:55:50.290874+00	\N
websearch	f	Web search	Cached perplexity-style web search response. Slug derived from the canonical query + model + freshness window. See src/precis/handlers/perplexity.py.	\N	2026-06-04 20:01:59.625687+00	\N
job	t	Job	Offline run of a task — fix this gripe, run a simulation, benchmark a commit. Addressable by numeric id; status via STATUS: tags; comment timeline via job_event + job_summary chunks.	\N	2026-10-05 07:16:44.174654+00	\N
pres	f	Presentation	Slide deck, unpublished writeup, or other internal document we want indexed but kept separate from the academic paper library. Slug-addressed; one block per slide (or per paragraph for writeups). Subtype carried as ``subtype:slides|writeup|notes|...`` open tag; ``venue`` and ``date`` live in meta. See ``precis-pres-help``.	\N	2026-10-05 07:16:44.179153+00	\N
cron	t	Cron	Scheduled wakeup. The cron-tick CLI scans due entries every 60s, fires pg_notify('precis.cron'), advances next_fire_at per recurrence + catch_up policy. Numeric-id; body lives as a ``cron_payload`` chunk. State in meta.next_fire_at, meta.recurring, meta.catch_up, meta.status. See ``precis-cron-help``.	\N	2026-10-05 07:16:44.181273+00	\N
message	t	Message	Proactive outbound. put(kind='message', target='discord/G/C/T', text='...') stores the ref AND fires pg_notify('precis.messages'). Delivery layer (asa_bot) LISTENs and posts. Numeric-id; one ref per send. Body as ``message_body`` chunk. State in meta.status: 'queued' → 'sent'/'failed'. See ``precis-message-help``.	\N	2026-10-05 07:16:44.181273+00	\N
flashcard	t	Flashcard	Spaced-repetition flashcard	\N	2026-05-21 20:06:05.179981+00	\N
perplexity-reasoning	f	Think	Cached perplexity ``think`` (chain-of-thought) response. Slug derived from the question + model + freshness window. See src/precis/handlers/perplexity.py.	\N	2026-06-04 20:01:59.625687+00	\N
perplexity-research	f	Research report	Cached perplexity ``research`` (deep-research) response. Slug derived from the prompt + model + freshness window. See src/precis/handlers/perplexity.py.	\N	2026-06-04 20:01:59.625687+00	\N
wikipedia	f	Wikipedia (on-demand article fetch)	Resolve a query to the best-matching Wikipedia article via the MediaWiki search API, then fetch and cache its plain-text extract. Slug-addressed by query; cached 7 days; block-split + embedded so search(kind='wikipedia', q=...) lands hits inside fetched articles. On-demand — no bulk dump, always current. See ``precis-wikipedia-help``.	\N	2026-10-05 07:16:44.196661+00	\N
alert	t	Alert	Machine-detected operational / health condition — a worker spin loop, an orphaned todo, a stalled recurring, a stale claim. Addressable by numeric id; deduped on meta.fingerprint; lifecycle via STATUS: tags (open / resolved); source + severity via alert-source: / severity: open tags. Not embedded — surfaced by the /alerts web tab, not semantic search.	\N	2026-10-05 07:16:44.200608+00	\N
draft	f	Draft	Editable, chunk-native authored document (ADR 0032). The living source of a project's write-up; exports to LaTeX/PDF/Word with Postgres canonical. Body chunks are mutable in structure (reorder/reparent via pos + parent_chunk_id) and in text (via the edit helper + content_sha re-derive). Named ref; chunks addressed by an opaque ¶<handle>. One draft per project; freeze = snapshot. See precis-draft-help.	\N	2026-10-05 07:16:44.202291+00	\N
news	f	News	Multi-source news aggregation. Articles pulled from RSS/Atom feeds (the news_sources registry) by the news_poll worker, fetched + extracted + embedded like web pages, so search(kind='news', q=...) lands hits inside article bodies. URL-addressed, pinned in cache. Tagged category:news + source:<slug> for filtering. The morning briefing summarizes recent items back out. See ``precis-news-help``.	\N	2026-10-05 07:16:44.204894+00	\N
agentlog	t	Agent log	Run-attribution record — one per agentic run (plan_tick, operator change request, chat follow-up) that touches the corpus. Carries the full assembled prompt, model + source, and `touched` links to every chunk the run wrote or moved, so a suspicious chunk can be walked back to the run that produced it. Numeric id; deduped per run; GC'd past a retention window (links drop, chunks stay). Not embedded — surfaced by the /agentlogs web tab and chunk connections, not semantic search. See ``precis-agentlog-help``.	\N	2026-10-05 07:16:44.206224+00	\N
finding	t	Finding	A retrievable empirical claim with explicit setup context and a provenance chain back to its primary source. Synthesised by the citation-chase worker; never externally citable (see docs/design/finding-chase.md).	\N	2026-05-30 21:33:14.261241+00	{scope,caveats}
memory	t	Memory	Note, decision, idea, claim	\N	2026-05-21 20:06:05.179981+00	{rule,warrant,hook}
orcid	f	ORCID author	A researcher identity resolved from ORCID (https://orcid.org). Slug-addressed by iD (e.g. 'orcid:0000-0002-1825-0097'). get resolves + stores the record (names, bio, keywords, employments with ROR ids), links works already held, and reports the missing ones — fetching them is LLM-gated via args={'enqueue': N}; search runs over the embedded author card; link/tag attach authorship edges (authored / authored-by) and classification. Durable link hub — never cache-evicted. See ``precis-orcid-help``.	\N	2026-10-05 07:16:44.211316+00	\N
cad	f	CAD	Parametric solid-model design (ADR 0041) — a boolean DAG of placed analytic primitives (box/cyl/cone/sphere/torus/prism/pyramid) authored via the compact `config` mini-DSL (e.g. cyl:r3h12). Postgres-canonical; the agent probes the model (point/ray/arc/section) and relates whole parts (clearance/interference/translational DOF) analytically rather than meshing. OpenSCAD/STL export is a regenerable downstream view. Named ref; nodes addressed by an opaque ca<id> handle. See precis-cad-help.	\N	2026-10-05 07:16:44.212655+00	\N
structure	f	Structure	Atomistic cell + bond-graph design for DFT/molecular modelling (ADR 0043). A periodic cell (lattice + per-axis PBC) filled with atoms (a<El><n> labels) and an explicit bond graph (order + provenance + periodic-image offset). The agent edits the graph via typed ops and probes it analytically (neighbours, coordination, MIC distances/angles, a validator gate) in memory — never pixels. Relaxation/DFT and file export (CIF/POSCAR/XYZ) are rented backends. Postgres-canonical; st<id> handle, design-scoped atom paths st<id>#a<El><n>. See precis-structure-help.	\N	2026-10-05 07:16:44.214164+00	\N
pcb	f	PCB	Electronics/PCB design (ADR 0042) — a netlist + placement graph in dedicated tables, read and authored by the LLM as a traversable graph (ratsnest / measures / signal-trace), never pixels. JLCPCB-native. Postgres-canonical; Freerouting/gerbers/fab are downstream export. See precis-pcb-help.	\N	2026-10-05 07:16:44.222384+00	\N
part	f	Part	LCSC/JLCPCB catalog part (ADR 0042) — reference data in the `parts` table, addressed by LCSC C-number. Ingest-only (jlcparts dump); not embedded. See precis-part-select-help.	\N	2026-10-05 07:16:44.222384+00	\N
datasheet	f	Datasheet	Component datasheet (ADR 0042) — a thin PaperHandler sibling (corpus_role=evidence) ingested via the Marker->chunks pipeline and linked datasheet-of a part. One kind for the whole electronics-doc family (app-note/errata via a meta sub-type). See precis-datasheet-help.	\N	2026-10-05 07:16:44.222384+00	\N
folder	t	Folder	Organizational container (ADR 0045): single-parent placement for authored artifacts via refs.parent_id and the reserved virtual `parent` link relation (ADR 0027, generalized). Folders organize what you MAKE — corpus kinds (paper/cfp) keep their own discovery layer and stream kinds (memory/alert/job) stay out. Shallow by policy. See precis-folder-help.	\N	2026-10-05 07:16:44.229943+00	\N
edgar	f	SEC Filing	Read-only SEC EDGAR filing (10-K / 10-Q / 8-K / S-1 / …). Accession-slugged (e.g. 0000320193-23-000106). Search merges local + EDGAR full-text; get(id=...) fetches the submissions index + primary document and stores section-labelled blocks. get(id='cik:320193' | 'ticker:aapl') lists a company's recent filings; view='diff' shows quarter-to-quarter section changes. See ``precis-edgar-help``.	\N	2026-10-05 07:16:44.234457+00	\N
plan	f	Plan	A thread's reasoning outline (ADR 0051 §2b) — a hierarchical todo-list + notes on the same chunk-tree substrate as a draft, addressed by pe<chunk_id>. Rendered whole with [open]/[wip]/done: status markers + a cursor; NEVER exported as a deliverable (corpus_role=none). One plan per project (plan-of link). See precis-overview.	\N	2026-10-05 07:16:44.2365+00	\N
figure	f	Figure	An interactive SVG canvas you draw *with* the model — a slug-addressed chunk-tree on the draft substrate, addressed by fg<ref>/fn<chunk>. Two model-owned documents: the SVG source (figure_node chunks) + a shared vocabulary (figure_vocab); chat persists as figure_turn. NEVER exported as a deliverable (corpus_role=none). Many per project (figure-of link). See precis-figure-help.	\N	2026-10-05 07:16:44.237187+00	\N
anki	t	Anki card	A spaced-repetition cloze card ({{c1::…}}) that lives in the corpus and syncs to AnkiWeb. Numeric-id ref; body is cloze markup, meta carries the generic Anki note shape (notetype/deck/fields). Anki owns scheduling — no SM-2 here. Supersedes flashcard. See precis-anki-help.	\N	2026-10-05 07:16:44.241097+00	\N
quest	t	Quest	A perpetual, unachievable striving (the medieval Grail sense) that pulls subtasks and knowledge acquisition into its service. Never `done` — lifecycle is active/dormant/abandoned. Achievable goals beneath it are ordinary todos/projects marked `serves`. Progress is a ledger of deeds, not a percentage. See docs/proposals/quest-layer.md.	\N	2026-10-05 07:16:44.245616+00	\N
mermaid	f	Mermaid	A mermaid diagram you draw *with* the model — a slug-addressed chunk-tree on the draft substrate, addressed by mm<ref>/mn<chunk>. Model-owned: the mermaid source (mermaid_node) + a shared vocabulary (mermaid_vocab) + private notes (mermaid_notes); chat persists as mermaid_turn. Nodes bind to the chunks they depict (ADR 0057). NEVER exported (corpus_role=none). Many per project (mermaid-of link). See precis-mermaid-help.	\N	2026-10-05 07:16:44.246425+00	\N
llm	t	LLM catalog	A model catalog card — one ref per model (claude-opus-4-8, qwen-heavy). Body is the capability prose (embedded, so the card is a vector); meta carries the structured facts (model_id, tier_floor, offerings, capability axes, provenance). A reconcile pass keeps the facts true against the live OpenRouter feed and flags drift. Read with get(kind='llm', id='claude-opus-4-8') or search(kind='llm', q=…). Never exported. See docs/proposals/llm-catalog.md.	\N	2026-10-05 07:16:44.250162+00	\N
material	f	Material	CRC-handbook-style engineering material properties store — a slug entity (name/aliases/class) plus per-property sourced values in a typed, growable property registry. v1 is canonical-units-only: a unit that is not the property's canonical unit is rejected, named. See precis-material-help.	\N	2026-10-05 07:16:44.269027+00	\N
component	f	Component	General procurable-part store — a slug entity (name/category/mpn/manufacturer) plus per-spec sourced values in a typed, growable, category-scoped spec registry. made-of links a component to the material it is made of. v1 is canonical-units-only, like material. See precis-component-help.	\N	2026-10-05 07:16:44.271449+00	\N
cfp	f	Call for Proposal	Call-for-proposal / requirements document. A read-only ingested PDF (via `precis add --as cfp` or the inbox/cfp/ watch dir) that a proposal draft must satisfy. Addressable by slug; one ref per document, blocks per chunk — gets search / TOC / keywords like a paper. Spec role: NEVER citable evidence (it is the requirements, not a source). Link it to a proposal project with link(rel='has-requirement') so the planner consults it. Use get(view='toc') to read the required sections + limits.	\N	2026-10-05 07:16:44.296422+00	\N
make	f	Make	A make-tree: assembly/synthesis order for a design — first-class step nodes on the draft chunk-tree substrate, each carrying its conditions (fixture/torque; reagents/temperature) in chunk meta. Blocks align to steps via made-by links written from the design side. Named ref; steps addressed mk<chunk_id>. See precis-cad-help.	\N	2026-10-05 07:16:44.34871+00	\N
rxn	f	Reaction	A sourced reaction-fact store: a transformation (reaction SMILES) plus per-property sourced values (yield, temperature, time, catalyst loading, ...) in a typed, growable registry. Many rows per (reaction, property) is the point — the spread across sources and conditions IS the answer. Named `rxn` not `reaction` to avoid colliding with the pathway graph's `reaction` edge kind. See precis-rxn-help.	\N	2026-10-05 07:16:44.35071+00	\N
checklist	f	Checklist	A named, versioned check ledger — Checklist-Manifesto-style argued gates for LLM agents. Items are judgment tasks or bridges to a domain's own encoded rules (DRC/ERC); per-target verdicts accumulate instead of restarting, and staleness (item revised, target changed) is rendered honestly rather than silently dropped. See precis-checklist-help.	\N	2026-10-05 07:16:44.353697+00	\N
concept	t	Concept	A node in the learner's personal knowledge graph (reading-prep loop): a term/idea with a continuous mastery field, derived state, embeddable definition, and typed edges (prerequisite / analogy / contrast) to other concepts. Objectives are concepts, not todos. See reading-prep-loop.md.	\N	2026-10-05 07:16:44.244247+00	{name,aliases,definition}
citation	t	Citation	Verified claim → source pointer. Written by the citation-fill workflow after the verifier confirms the source quote supports the claim.	\N	2026-05-31 14:47:51.530091+00	{claim,source_quote,char_offset,source_handle}
taxon	t	Taxon	A node in the term taxonomy: a named term with an embeddable definition, an earned status (proposed / systematic), an optional dimension (dimension_kind + si_vector) and, on start nodes, a required-key contract. Body is '<name> - <definition>'. See term-taxonomy.md.	\N	2026-10-05 07:16:44.38315+00	{name,aliases,definition,status,value_type,dimension_kind,canonical_unit,allowed_values,higher_is_better,applies_to_ref,si_vector,required_conditions,display_unit}
\.


--
-- Data for Name: material_properties; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.material_properties (prop_id, name, canonical_unit, dimension, value_type, allowed_values, standard_ref, status, higher_is_better, description, created_at) FROM stdin;
density	Density	kg/m3	mass/volume	quantity	\N	\N	core	\N	Mass per unit volume.	2026-10-05 07:16:44.269027+00
tensile_strength_yield	Tensile yield strength	MPa	pressure/stress	quantity	\N	\N	core	\N	Stress at the onset of plastic deformation (0.2% offset).	2026-10-05 07:16:44.269027+00
tensile_strength_ultimate	Ultimate tensile strength	MPa	pressure/stress	quantity	\N	\N	core	\N	Maximum engineering stress before necking/fracture.	2026-10-05 07:16:44.269027+00
youngs_modulus	Young's modulus	GPa	pressure/stress	quantity	\N	\N	core	\N	Elastic (tensile/compressive) stiffness.	2026-10-05 07:16:44.269027+00
shear_modulus	Shear modulus	GPa	pressure/stress	quantity	\N	\N	core	\N	Elastic shear stiffness.	2026-10-05 07:16:44.269027+00
poissons_ratio	Poisson's ratio	\N	dimensionless	ratio	\N	\N	core	\N	Negative ratio of transverse to axial strain.	2026-10-05 07:16:44.269027+00
elongation_at_break	Elongation at break	%	dimensionless	ratio	\N	\N	core	\N	Engineering strain at fracture in a tensile test.	2026-10-05 07:16:44.269027+00
hardness_vickers	Vickers hardness	HV	hardness (non-convertible scale)	quantity	\N	\N	core	\N	Indentation hardness on the Vickers scale.	2026-10-05 07:16:44.269027+00
thermal_conductivity	Thermal conductivity	W/(m*K)	power/(length*temperature)	quantity	\N	\N	core	\N	Rate of heat transfer through a unit thickness per unit temperature gradient.	2026-10-05 07:16:44.269027+00
specific_heat_capacity	Specific heat capacity	J/(kg*K)	energy/(mass*temperature)	quantity	\N	\N	core	\N	Heat required to raise unit mass by one kelvin.	2026-10-05 07:16:44.269027+00
thermal_expansion_coeff	Coefficient of thermal expansion	1/K	1/temperature	quantity	\N	\N	core	\N	Fractional length change per kelvin (linear CTE).	2026-10-05 07:16:44.269027+00
melting_point	Melting point	K	temperature	quantity	\N	\N	core	\N	Solid-to-liquid transition temperature. Absolute scale (Kelvin).	2026-10-05 07:16:44.269027+00
max_service_temperature	Maximum service temperature	K	temperature	quantity	\N	\N	core	\N	Upper continuous-use temperature before properties degrade. Absolute scale.	2026-10-05 07:16:44.269027+00
electrical_resistivity	Electrical resistivity	ohm*m	resistance*length	quantity	\N	\N	core	\N	Bulk resistivity to electrical current.	2026-10-05 07:16:44.269027+00
dielectric_strength	Dielectric strength	MV/m	voltage/length	quantity	\N	\N	core	\N	Maximum electric field before insulation breakdown.	2026-10-05 07:16:44.269027+00
relative_permittivity	Relative permittivity	\N	dimensionless	ratio	\N	\N	core	\N	Permittivity relative to vacuum (dielectric constant).	2026-10-05 07:16:44.269027+00
cost_per_mass	Cost per unit mass	USD/kg	currency/mass	quantity	\N	\N	core	\N	Market cost per unit mass; pair with as_of (load-bearing for cost).	2026-10-05 07:16:44.269027+00
crystal_structure	Crystal structure	\N	categorical	categorical	["FCC", "BCC", "HCP"]	\N	proposed	\N	Crystallographic lattice type.	2026-10-05 07:16:44.269027+00
is_magnetic	Is magnetic	\N	categorical	boolean	\N	\N	proposed	\N	Whether the material is ferro/ferrimagnetic at room temperature.	2026-10-05 07:16:44.269027+00
\.


--
-- Data for Name: measure_unit_compat; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.measure_unit_compat (legacy_table, legacy_key, legacy_unit, si_unit, factor, si_offset) FROM stdin;
component_specs	across_flats	mm	m	0.001	0
component_specs	bore_diameter	mm	m	0.001	0
component_specs	bore_diameter_bearing	mm	m	0.001	0
component_specs	drive_size	mm	m	0.001	0
component_specs	head_diameter	mm	m	0.001	0
component_specs	head_height	mm	m	0.001	0
component_specs	height	mm	m	0.001	0
component_specs	inner_diameter	mm	m	0.001	0
component_specs	length	mm	m	0.001	0
component_specs	min_bend_radius	mm	m	0.001	0
component_specs	outer_diameter	mm	m	0.001	0
component_specs	thickness	mm	m	0.001	0
component_specs	thread_pitch	mm	m	0.001	0
component_specs	wall_thickness	mm	m	0.001	0
component_specs	width	mm	m	0.001	0
component_specs	max_working_pressure	MPa	Pa	1000000	0
component_specs	head_angle	deg	rad	0.01745329251994329577	0
material_properties	persistence_length	nm	m	0.000000001	0
material_properties	unit_length	nm	m	0.000000001	0
material_properties	delta_length	Å	m	0.0000000001	0
material_properties	tensile_strength_ultimate	MPa	Pa	1000000	0
material_properties	tensile_strength_yield	MPa	Pa	1000000	0
material_properties	shear_modulus	GPa	Pa	1000000000	0
material_properties	youngs_modulus	GPa	Pa	1000000000	0
material_properties	dielectric_strength	MV/m	V/m	1000000	0
material_properties	elongation_at_break	%	1	0.01	0
rxn_properties	atom_economy	%	1	0.01	0
rxn_properties	ee	%	1	0.01	0
rxn_properties	yield	%	1	0.01	0
rxn_properties	catalyst_loading	mol%	1	0.01	0
\.


--
-- Data for Name: news_sources; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.news_sources (source_id, url, title, source_slug, category, default_tags, max_items, enabled, etag, last_modified, last_polled_at, last_status, consecutive_errors, created_at) FROM stdin;
1	https://feeds.bbci.co.uk/news/world/rss.xml	BBC News — World	bbc	world	{}	50	t	\N	\N	\N	\N	0	2026-10-05 07:16:44.204894+00
2	https://feeds.npr.org/1001/rss.xml	NPR — News	npr	world	{}	50	t	\N	\N	\N	\N	0	2026-10-05 07:16:44.204894+00
3	https://www.theguardian.com/world/rss	The Guardian — World	guardian	world	{}	50	t	\N	\N	\N	\N	0	2026-10-05 07:16:44.204894+00
4	https://feeds.arstechnica.com/arstechnica/index	Ars Technica	arstechnica	tech	{topic:tech}	50	t	\N	\N	\N	\N	0	2026-10-05 07:16:44.204894+00
5	https://hnrss.org/frontpage	Hacker News — Front Page	hn	tech	{topic:tech}	50	t	\N	\N	\N	\N	0	2026-10-05 07:16:44.204894+00
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
wikipedia	Wikipedia / MediaWiki API (search + plain-text extracts)	\N	2026-10-05 07:16:44.196661+00
news	RSS / Atom news feeds (news_sources registry)	\N	2026-10-05 07:16:44.204894+00
orcid	ORCID Public API (https://pub.orcid.org/v3.0/) — author identity + works	\N	2026-10-05 07:16:44.211316+00
sec_edgar	US SEC EDGAR — company filings (submissions + archive APIs)	\N	2026-10-05 07:16:44.234457+00
sec_edgar_search	US SEC EDGAR — full-text search (efts.sec.gov)	\N	2026-10-05 07:16:44.234457+00
markup	Structured full-text ingest (JATS / Elsevier XML / arXiv HTML / LaTeX)	\N	2026-10-05 07:16:44.248622+00
\.


--
-- Data for Name: relations; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.relations (slug, is_symmetric, inverse_slug, description, deprecated_at, created_at, domain_kinds, range_kinds, functional, transitive, acyclic) FROM stdin;
related-to	t	\N	Symmetric association	\N	2026-05-21 20:06:05.179981+00	\N	\N	f	f	f
blocks	f	blocked-by	Source blocks target	\N	2026-05-21 20:06:05.179981+00	\N	\N	f	f	f
blocked-by	f	blocks	Source is blocked by target	\N	2026-05-21 20:06:05.179981+00	\N	\N	f	f	f
contradicted-by	f	contradicts	Source is contradicted by target	\N	2026-05-21 20:06:05.179981+00	\N	\N	f	f	f
cites	f	cited-by	Source cites target	\N	2026-05-21 20:06:05.179981+00	\N	\N	f	f	f
cited-by	f	cites	Source is cited by target	\N	2026-05-21 20:06:05.179981+00	\N	\N	f	f	f
supersedes	f	superseded-by	Source supersedes target	\N	2026-05-21 20:06:05.179981+00	\N	\N	f	f	f
superseded-by	f	supersedes	Source is superseded by target	\N	2026-05-21 20:06:05.179981+00	\N	\N	f	f	f
retracted-by	f	retracts	Source is retracted by target (retraction notice)	\N	2026-05-30 16:07:11.520836+00	\N	\N	f	f	f
retracts	f	retracted-by	Source retracts target	\N	2026-05-30 16:07:11.520836+00	\N	\N	f	f	f
corrected-by	f	corrects	Source is corrected by target (corrigendum/erratum/addendum)	\N	2026-05-30 16:07:11.520836+00	\N	\N	f	f	f
corrects	f	corrected-by	Source corrects target	\N	2026-05-30 16:07:11.520836+00	\N	\N	f	f	f
concern-raised-by	f	raises-concern-about	Source has an Expression of Concern attached	\N	2026-05-30 16:07:11.520836+00	\N	\N	f	f	f
raises-concern-about	f	concern-raised-by	Source raises concern about target	\N	2026-05-30 16:07:11.520836+00	\N	\N	f	f	f
misattributes	f	misattributed-by	Source chunk misrepresents what the target chunk actually says	\N	2026-05-30 21:33:14.261241+00	\N	\N	f	f	f
misattributed-by	f	misattributes	Source chunk is misrepresented by the linked source chunk	\N	2026-05-30 21:33:14.261241+00	\N	\N	f	f	f
derived-from	f	derived-into	Source is derived from target (cause/origin)	\N	2026-05-31 18:20:12.906601+00	\N	\N	f	f	f
derived-into	f	derived-from	Source is the origin from which target derives	\N	2026-05-31 18:20:12.906601+00	\N	\N	f	f	f
supports	f	supported-by	Source provides evidence for target	\N	2026-05-31 18:20:12.906601+00	\N	\N	f	f	f
supported-by	f	supports	Source is supported by target	\N	2026-05-31 18:20:12.906601+00	\N	\N	f	f	f
generalises	f	specialises	Source is a generalisation of target	\N	2026-05-31 18:20:12.906601+00	\N	\N	f	f	f
see-also	f	\N	One-way "for context" pointer (no inverse)	\N	2026-05-31 18:20:12.906601+00	\N	\N	f	f	f
fixes	f	fixed-by	Source ref offers a fix for the target ref (e.g. a fix_gripe job → its gripe)	\N	2026-10-05 07:16:44.175577+00	\N	\N	f	f	f
fixed-by	f	fixes	Source ref is being fixed by the target ref	\N	2026-10-05 07:16:44.175577+00	\N	\N	f	f	f
has-draft	f	draft-of	Source project (todo) has target draft as its working document.	\N	2026-10-05 07:16:44.204246+00	\N	\N	f	f	f
snapshot-of	f	has-snapshot	Source frozen ref is a point-in-time snapshot of target draft.	\N	2026-10-05 07:16:44.204246+00	\N	\N	f	f	f
has-snapshot	f	snapshot-of	Source draft has target frozen ref as a snapshot.	\N	2026-10-05 07:16:44.204246+00	\N	\N	f	f	f
touched	t	\N	Source agent run wrote or moved target chunk (run-attribution). Symmetric for graph purposes — surfaced from either end.	\N	2026-10-05 07:16:44.206224+00	\N	\N	f	f	f
plots	f	plotted-by	Source figure chunk renders the target data chunk — the figure plots that data. The one reactive edge: editing the data marks the figure stale (ADR 0035).	\N	2026-10-05 07:16:44.209443+00	\N	\N	f	f	f
plotted-by	f	plots	Source data chunk is rendered by the target figure chunk (inverse of plots).	\N	2026-10-05 07:16:44.209443+00	\N	\N	f	f	f
authored	f	authored-by	Source author node (kind=orcid) authored the target paper. Ref-level edge; meta carries best-effort author_position / n_authors when known (ADR 0039).	\N	2026-10-05 07:16:44.210689+00	\N	\N	f	f	f
authored-by	f	authored	Source paper was authored by the target author node (inverse of authored).	\N	2026-10-05 07:16:44.210689+00	\N	\N	f	f	f
has-requirement	f	requirement-of	Source project (todo) must satisfy target call-for-proposal (cfp).	\N	2026-10-05 07:16:44.212017+00	\N	\N	f	f	f
requirement-of	f	has-requirement	Source call-for-proposal (cfp) is a requirement of target project.	\N	2026-10-05 07:16:44.212017+00	\N	\N	f	f	f
requested	f	requested-by	Source todo requested target derived job and waits on it.	\N	2026-10-05 07:16:44.221778+00	\N	\N	f	f	f
requested-by	f	requested	Source derived job was requested by target todo.	\N	2026-10-05 07:16:44.221778+00	\N	\N	f	f	f
datasheet-of	f	has-datasheet	Source datasheet documents target part (evidence for its specs).	\N	2026-10-05 07:16:44.235123+00	\N	\N	f	f	f
has-datasheet	f	datasheet-of	Source part is documented by target datasheet.	\N	2026-10-05 07:16:44.235123+00	\N	\N	f	f	f
has-plan	f	plan-of	Source project (todo) has target plan as its reasoning outline.	\N	2026-10-05 07:16:44.2365+00	\N	\N	f	f	f
figure-of	f	has-figure	Source figure belongs to target project (todo). Many-per-project.	\N	2026-10-05 07:16:44.237187+00	\N	\N	f	f	f
has-figure	f	figure-of	Source project (todo) has target figure. Many-per-project.	\N	2026-10-05 07:16:44.237187+00	\N	\N	f	f	f
prerequisite-of	f	has-prerequisite	Source concept is a prerequisite of (must be learned before) target.	\N	2026-10-05 07:16:44.244247+00	\N	\N	f	f	f
analogy-of	t	\N	Source and target concepts are analogous — teach one via the other.	\N	2026-10-05 07:16:44.244247+00	\N	\N	f	f	f
contrasts-with	t	\N	Source and target concepts are confusably similar but distinct.	\N	2026-10-05 07:16:44.244247+00	\N	\N	f	f	f
represents	f	represented-by	Source concept is rendered by target card (an anki/other representation).	\N	2026-10-05 07:16:44.244247+00	\N	\N	f	f	f
represented-by	f	represents	Source card renders (is a representation of) target concept.	\N	2026-10-05 07:16:44.244247+00	\N	\N	f	f	f
depicts	f	depicted-in	A diagram (figure/mermaid) source chunk depicts the target chunk/ref it illustrates; the depicting element id(s) live in links.meta.elements. Diagram→corpus binding (ADR 0057), the element-granular cousin of plots.	\N	2026-10-05 07:16:44.244934+00	\N	\N	f	f	f
depicted-in	f	depicts	Source chunk/ref is depicted by the target diagram (inverse of depicts, ADR 0057).	\N	2026-10-05 07:16:44.244934+00	\N	\N	f	f	f
served-by	f	serves	Source quest is served by the target work/knowledge node.	\N	2026-10-05 07:16:44.245616+00	\N	\N	f	f	f
mermaid-of	f	has-mermaid	Source mermaid diagram belongs to target project (todo). Many-per-project.	\N	2026-10-05 07:16:44.246425+00	\N	\N	f	f	f
has-mermaid	f	mermaid-of	Source project (todo) has target mermaid diagram. Many-per-project.	\N	2026-10-05 07:16:44.246425+00	\N	\N	f	f	f
has-dossier	f	dossier-of	Source quest has the target draft as its research dossier.	\N	2026-10-05 07:16:44.247149+00	\N	\N	f	f	f
entails	f	entailed-by	Source inference node logically yields the target conclusion lemma (asserted, not proven).	\N	2026-10-05 07:16:44.259112+00	\N	\N	f	f	f
entailed-by	f	entails	Source lemma is the asserted conclusion of the target inference node.	\N	2026-10-05 07:16:44.259112+00	\N	\N	f	f	f
qualifies	f	qualified-by	Source caveat node limits/bounds the target claim (finding or lemma).	\N	2026-10-05 07:16:44.259112+00	\N	\N	f	f	f
qualified-by	f	qualifies	Source claim is limited/bounded by the target caveat node.	\N	2026-10-05 07:16:44.259112+00	\N	\N	f	f	f
cited-in	f	\N	Paper is woven into and cited by the document; a citation exists. src=paper, dst=dossier draft (optionally its section chunk).	\N	2026-10-05 07:16:44.263362+00	\N	\N	f	f	f
superseded-in	f	\N	Paper is subsumed by a later or review paper already integrated; recorded, not separately woven.	\N	2026-10-05 07:16:44.263362+00	\N	\N	f	f	f
off-topic-for	f	\N	Paper was considered for the document and rejected as out of scope.	\N	2026-10-05 07:16:44.263362+00	\N	\N	f	f	f
copy-of	f	has-copy	Source draft is a fork/deep-copy of target draft (chunks + links copied).	\N	2026-10-05 07:16:44.266115+00	\N	\N	f	f	f
has-copy	f	copy-of	Source draft has target draft as a fork/deep-copy of itself.	\N	2026-10-05 07:16:44.266115+00	\N	\N	f	f	f
paper-of	f	has-paper	Source draft is the reader-facing paper projection of the target quest/process's dossier — a separate draft from the dossier itself.	\N	2026-10-05 07:16:44.266796+00	\N	\N	f	f	f
has-paper	f	paper-of	Source quest/process has the target draft as its reader-facing paper.	\N	2026-10-05 07:16:44.266796+00	\N	\N	f	f	f
made-of	f	used-in	Source component is made of target material.	\N	2026-10-05 07:16:44.271449+00	\N	\N	f	f	f
used-in	f	made-of	Source material is used in target component.	\N	2026-10-05 07:16:44.271449+00	\N	\N	f	f	f
part-of	f	contains	Source component is structurally part of target component.	\N	2026-10-05 07:16:44.275202+00	\N	\N	f	f	f
refines	f	\N	Source claim hub is a sharper/reworded version of the target claim hub (taproot claim→claim advisory link; link-don't-merge, no evidence flow).	\N	2026-10-05 07:16:44.27948+00	\N	\N	f	f	f
awaits-evidence	f	\N	An acquisition-mode finding (STATUS:acquiring) awaits corpus evidence from the linked DREAM:acquire paper stub.	\N	2026-10-05 07:16:44.284028+00	\N	\N	f	f	f
same-family-as	t	\N	Both patent refs are members of the same EPO OPS DOCDB patent family; source is typically a stub ingest, target the family's current publication-date representative.	\N	2026-10-05 07:16:44.29459+00	\N	\N	f	f	f
conjunct-of	f	\N	Source claim hub is one atomic conjunct of the target compound claim hub (taproot claim→claim advisory link; link-don't-merge, no evidence flow).	\N	2026-10-05 07:16:44.304289+00	\N	\N	f	f	f
motivated-by	f	\N	Source hypothesis claim hub was provoked by the target artifact (paper, patent, or claim hub) — taproot advisory link; motivation, NOT evidence, and no evidence flows along it.	\N	2026-10-05 07:16:44.319636+00	\N	\N	f	f	f
tests	f	\N	Source measurement artifact (computed pathway) executed the target hypothesis finding's pre-registered discriminating experiment — quest dialectic measurement-ruling edge; NOT evidence, and no evidence flows along it (sim rulings settle internal hypotheses only).	\N	2026-10-05 07:16:44.334302+00	\N	\N	f	f	f
disputes	f	\N	Source artifact appears to conflict with the target — a non-blocking open question, free to file, resolved only by adjudication (which alone may derive a blocking `contradicts`).	\N	2026-10-05 07:16:44.345909+00	\N	\N	f	f	f
analyzed-by	f	analysis-of	Source design/block is analyzed by the target result (finding/estimate with fidelity + validity scope); links.meta {sha, at} pins the analyzed design version.	\N	2026-10-05 07:16:44.348015+00	\N	\N	f	f	f
analysis-of	f	analyzed-by	Source analysis result describes the target design/block.	\N	2026-10-05 07:16:44.348015+00	\N	\N	f	f	f
made-by	f	makes	Source design/block is produced by the target make-tree (ref-level) or make-step (chunk-scoped); many-to-many — make-order need not align with design structure.	\N	2026-10-05 07:16:44.34871+00	\N	\N	f	f	f
makes	f	made-by	Source make-tree/step produces the target design/block.	\N	2026-10-05 07:16:44.34871+00	\N	\N	f	f	f
realized-by	f	realizes	Source ref is made real by the target (e.g. a cad design's catalog part -> the procurable component that realizes it)	\N	2026-10-05 07:16:44.350074+00	\N	\N	f	f	f
realizes	f	realized-by	Source ref makes the target real (e.g. a procurable component -> the design that calls for it)	\N	2026-10-05 07:16:44.350074+00	\N	\N	f	f	f
instance-of	f	has-instance	Source ref is a member of the target taxon (e.g. a material, a measured value -> the term it instantiates).	\N	2026-10-05 07:16:44.38315+00	\N	\N	f	f	f
has-instance	f	instance-of	Source taxon has the target ref as a member.	\N	2026-10-05 07:16:44.38315+00	\N	\N	f	f	f
contradicts	f	contradicted-by	Source contradicts target. Claim-graph contradicts is adjudication-derived and cannot be filed manually — file rel='disputes' instead (free, non-blocking). Only memory-to-memory contradicts is fileable.	\N	2026-05-21 20:06:05.179981+00	{memory}	{memory}	f	f	f
draft-of	f	has-draft	Source draft is the working document of target project (todo).	\N	2026-10-05 07:16:44.204246+00	\N	\N	t	f	f
plan-of	f	has-plan	Source plan is the reasoning outline of target project (todo).	\N	2026-10-05 07:16:44.2365+00	\N	\N	t	f	f
dossier-of	f	has-dossier	Source draft is the research dossier of the target quest — the living synthesis rewritten each cycle, and the loop's rolling context.	\N	2026-10-05 07:16:44.247149+00	\N	\N	t	f	f
establishes	f	\N	Source paper first showed / originated the target claim (taproot evidence edge; originator).	\N	2026-10-05 07:16:44.274517+00	{datasheet,edgar,paper,patent,pathway}	{finding}	f	f	f
corroborates	f	\N	Paper supports an existing point in the document, grouped with it.	\N	2026-10-05 07:16:44.263362+00	{datasheet,edgar,paper,patent,pathway}	{draft,finding}	f	f	f
specialises	f	generalises	Source is a specialisation of target	\N	2026-05-31 18:20:12.906601+00	\N	\N	f	t	t
has-prerequisite	f	prerequisite-of	Source concept requires target concept first (the learning DAG).	\N	2026-10-05 07:16:44.244247+00	\N	\N	f	t	t
serves	f	served-by	Source (project/todo/concept/paper/job/draft/structure/sub-quest) is in the service of the target quest — the striving DAG above the todo tree.	\N	2026-10-05 07:16:44.245616+00	\N	\N	f	t	t
contains	f	part-of	Source component structurally contains target component (BOM edge).	\N	2026-10-05 07:16:44.275202+00	\N	\N	f	t	t
quantifies	f	quantified-by	Source paper (chunk-scoped) states a number for the target measurand taxon. One shared edge per (chunk, measurand); each measure keeps its own anchor_scheme + span on its row, and measures.primary_link_id points at the edge.	\N	2026-10-05 07:16:44.405891+00	\N	\N	f	f	f
quantified-by	f	quantifies	Source measurand taxon is quantified by the target paper chunk.	\N	2026-10-05 07:16:44.405891+00	\N	\N	f	f	f
\.


--
-- Data for Name: rxn_properties; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.rxn_properties (prop_id, name, canonical_unit, dimension, value_type, allowed_values, standard_ref, status, higher_is_better, description, created_at) FROM stdin;
yield	Yield	%	dimensionless	ratio	\N	\N	core	t	Fraction of theoretical product obtained. Isolated vs assay/NMR yield is a conditions key, not a separate property.	2026-10-05 07:16:44.35071+00
temperature	Temperature	K	temperature	quantity	\N	\N	core	\N	Reaction temperature. Absolute scale (Kelvin).	2026-10-05 07:16:44.35071+00
time	Reaction time	s	time	quantity	\N	\N	core	f	Elapsed reaction time.	2026-10-05 07:16:44.35071+00
pressure	Pressure	Pa	pressure	quantity	\N	\N	core	\N	Reaction pressure.	2026-10-05 07:16:44.35071+00
catalyst_loading	Catalyst loading	mol%	dimensionless	ratio	\N	\N	core	f	Catalyst charge relative to limiting reagent.	2026-10-05 07:16:44.35071+00
scale	Scale	mol	amount	quantity	\N	\N	core	\N	Amount of limiting reagent. A yield at 1 mmol and at 1 mol are different claims.	2026-10-05 07:16:44.35071+00
ee	Enantiomeric excess	%	dimensionless	ratio	\N	\N	core	t	Enantiomeric excess of the product.	2026-10-05 07:16:44.35071+00
atom_economy	Atom economy	%	dimensionless	ratio	\N	\N	core	t	Mass of desired product over summed mass of reactants. Computable from stoichiometry alone.	2026-10-05 07:16:44.35071+00
solvent	Solvent	\N	dimensionless	text	\N	\N	core	\N	Reaction solvent. Text in v1; a solvent entity is a later refinement.	2026-10-05 07:16:44.35071+00
price_per_gram	Price per gram	USD/g	currency/mass	quantity	\N	\N	core	f	Purchase price of a buyable compound. A price is a property of (compound, vendor, pack size, date) — vendor and pack size belong in conditions, the date in as_of, and the source licence in source_licence.	2026-10-05 07:16:44.35071+00
\.


--
-- Data for Name: summarizers; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.summarizers (name, prompt_template, config, is_default, description, deprecated_at, created_at) FROM stdin;
rake-lemma	\N	{"model": "en_core_sci_sm", "lemmatizer": "scispacy", "max_keywords": 50, "max_phrase_words": 4, "min_phrase_words": 1}	t	RAKE phrase extraction + scispacy lemmatisation	\N	2026-05-21 20:06:05.179981+00
llm-v1	\N	{"alias": "summarizer", "model": "qwen3-next-80b-a3b", "format": "brief;detail", "version": "1", "endpoint": "local"}	f	LLM brief+detail chunk summary (Qwen3-Next-80B-A3B via the litellm `summarizer` alias)	\N	2026-10-05 07:16:44.19603+00
\.


--
-- Name: news_sources_source_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.news_sources_source_id_seq', 5, true);


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
0150_struct_atoms_charge	1970-01-01 00:00:00+00	f1d641187a7c3268439a977e6b23d21932125d802b23d41eeabf40c20f4f0a0e	precis
0151_disputes_relation	1970-01-01 00:00:00+00	3a9242e0eceb061088a1beb213202e06cbb6e463ad6ae7a067493868e5bca801	precis
0152_component_geometry_specs	1970-01-01 00:00:00+00	af1a9e619fb1483d9db0e46d6831d2a70fc11a42c3db0294ed4af056b28f6ab7	precis
0153_analyzed_by_relation	1970-01-01 00:00:00+00	2f5526ee8977c3a660911402f320e41a653864e28709003fe135b10377c3f3a5	precis
0154_make_kind_and_made_by	1970-01-01 00:00:00+00	33c8d12328a20b8002bd405a22877bc942891ed3a368ab693d0ec2ef8246a758	precis
0155_reseed_fixes_relation	1970-01-01 00:00:00+00	6466146a3bfc0c4f198f42622686aeeef1576d7ad7cb9c57c1c75ca205c8c25b	precis
0156_realizes_relation	1970-01-01 00:00:00+00	298b19d04c60fe8316dd23219ad95b264836861da19c4c062fc9f5915e9d168a	precis
0157_rxn_kind	1970-01-01 00:00:00+00	0830d7c95d2416af36f89616aa8f925ff9607c4642d0b28931b5128bd1913aa7	precis
0158_checklist_kind	1970-01-01 00:00:00+00	54d553d1deb1d82f5433196bf919bb75dff9aa940cb6a7e81e33a97a8aef591b	precis
0159_units_cad_wipe	1970-01-01 00:00:00+00	92354da8b1c6851c06388e09d6de01079eaec822d30be3249177d6419f1567b8	precis
0160_pcb_local_footprints	1970-01-01 00:00:00+00	5f7602180e33c37c38e5f1740489ffaa8452bab06e566fce93e2694ed2a84644	precis
0161_pcb_generators	1970-01-01 00:00:00+00	f6226145581e36b2116eef0607bb8016c23597dbd09597341054f46b89838ece	precis
0162_design_core	1970-01-01 00:00:00+00	2e2cca13584b06ca5d297a3883a65782533828ee54470af84bb0455e6ab393da	precis
0163_component_head_form_specs	1970-01-01 00:00:00+00	a956429a34e1a5c63746eb8a575c6d074ff2d80f0d6282299d13fbdd12889375	precis
0164_refs_owner_login	1970-01-01 00:00:00+00	e931526da84ef3da3fb55f1292f7a0680790f2ec81158a47d8dba85f6307e699	precis
0165_pcb_fixed_copper	1970-01-01 00:00:00+00	081dfd63b90428c9471c62fb4e15a05168da919bce00cfb6d217d906378c1fc4	precis
0166_refs_owner_login_validate	1970-01-01 00:00:00+00	761dc9513939ecd702a2a2f45d868b77da80da984c07b162adbbcf0921ec8912	precis
0167_design_transition_requires	1970-01-01 00:00:00+00	fa8fd746927c1a454f36ac07a477cd3f2a510aa99edde8aff81567d4a3677d16	precis
0168_paper_authors	1970-01-01 00:00:00+00	45dfadd97cb115bfd40a8a3c07b02193c5d37748baf744fc32d38696a65ee441	precis
0169_design_revisions	1970-01-01 00:00:00+00	f6cc119dcc6c6ea6c5dcd74a457674cfa4131c4c7a10a140207028406ec27696	precis
0170_chunk_kind_field	1970-01-01 00:00:00+00	6be6425673155eba69d327640f714e0c8de7e0bc4641ec8fd9133db0c65dba5c	precis
0171_pcb_net_electrical_spec	1970-01-01 00:00:00+00	121b92f014c2a791d4510086ad0dab1b8488230785a7313303a7b7152bc5221b	precis
0172_design_states_occupancy_pose	1970-01-01 00:00:00+00	55efb1d10f35e0ccaff30e49656e36c596b70b50544ae7ccbf00d3ad1c9d8e83	precis
0173_taxon_kind	1970-01-01 00:00:00+00	dfcc57b821eadc5d835b0fc7c3c8397e5c07887570d6c401b5590957167d43d0	precis
0174_taxon_seed	1970-01-01 00:00:00+00	756ccd05d4457c4f7a5af4eca6941db9d3a62c7efd69aab9e4c994fe389ceed4	precis
0175_chase_coverage	1970-01-01 00:00:00+00	4c080cfa683b6fdd60ca4e7a7985274f415bdffc821039436b4ba6d905c84376	precis
0176_gripe_status_required	1970-01-01 00:00:00+00	f23b04081b0aab34a9fe3a0427cca2c43f41f984c0f203123183e3b5476e75d8	precis
0179_llm_call_log_placement_routed	1970-01-01 00:00:00+00	2c0cafc29dbb3f4e628d9dd716cb25cb61702cf0dc10a483561cefe54335e0e9	precis
0180_relation_constraints	1970-01-01 00:00:00+00	a112d544b9c73564cb98ea022e1f200068248c2b6f2822b5bcbcedb357103e78	precis
0181_nanopub_composite_artifact_type	1970-01-01 00:00:00+00	ba81150e776a2caeaccf4776ac39c538528770dcf34464addfd5fc89d3b97f29	precis
0182_se_measurand_seed	1970-01-01 00:00:00+00	7a69406c89551ccfbf3c9d9cb72ec4ce160e583b168857f633dbfabf80c5ce4e	precis
0185_reviews_and_revisions	1970-01-01 00:00:00+00	ac01adf7f672348a939c602401b9f2311bed274ef563e76a5c8a61303122afcd	precis
0187_measures	1970-01-01 00:00:00+00	09f985f24dd07552bc4b41d4d9b0938b098f0e7e0359f3eebdd5afe208a4e963	precis
0188_measures_si	1970-01-01 00:00:00+00	f1e223ece582b6a4e49ebe62ff1f08c22e17835be4be8836663798c07f234a19	precis
0189_secret_hint_counts	1970-01-01 00:00:00+00	f736c84f3266e5f2c896936265113ebb8397cdbb47e97a375e630b444e3175dc	precis
\.
