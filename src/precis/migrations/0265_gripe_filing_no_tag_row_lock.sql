-- 0265_gripe_filing_no_tag_row_lock.sql
--
-- gr537784: gripe filing failed with LockNotAvailable under prod's
-- lock_timeout=5s. 0244's ``file_gripe_readonly(text, text)`` resolved each
-- tag id with ``INSERT ... ON CONFLICT DO UPDATE SET namespace =
-- EXCLUDED.namespace RETURNING tag_id`` — a no-op UPDATE of a key column,
-- so a FOR UPDATE row lock on the shared ``STATUS:open`` tag row held to
-- commit. It serialized every filing against every other transaction that
-- tagged STATUS:open, and against any open ``ref_tags`` insert's FK KEY
-- SHARE lock on that row.
--
-- Same body as 0244, except each tag id is now read first (no row lock);
-- only a brand-new tag inserts, with DO NOTHING plus a re-read covering a
-- concurrent first insert. The Python twin is
-- ``precis.store._mappers._upsert_tag``. The one-arg wrapper delegates
-- here, so it needs no change.
--
-- Forward-only (ADR 0005). Idempotent (CREATE OR REPLACE).

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE FUNCTION public.file_gripe_readonly(p_text text, p_source text)
    RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_ref_id bigint;
    v_tag_id bigint;
    v_ro boolean;
    v_role text;
    v_n integer;
    v_ns text;
    v_val text;
BEGIN
    IF p_text IS NULL OR length(btrim(p_text)) = 0 THEN
        RAISE EXCEPTION 'file_gripe_readonly: text must not be empty';
    END IF;
    IF p_source IS NOT NULL AND p_source !~ '^[a-z0-9_.:-]{1,48}$' THEN
        RAISE EXCEPTION 'file_gripe_readonly: source label % must match [a-z0-9_.:-]{1,48}', p_source;
    END IF;

    v_role := COALESCE(NULLIF(current_setting('role'), 'none'), session_user);
    v_ro := NOT has_table_privilege(v_role, 'refs', 'INSERT');

    IF v_ro OR p_source IS NOT NULL THEN
        IF length(p_text) > 4000 THEN
            RAISE EXCEPTION 'file_gripe_readonly: text exceeds the 4000-char limit (got % chars)', length(p_text);
        END IF;
        PERFORM pg_advisory_xact_lock(hashtext('file_gripe_readonly'));
        IF p_source IS NOT NULL THEN
            SELECT count(*) INTO v_n
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'gripe'
               AND t.namespace = 'OPEN'
               AND t.value = 'agent:' || p_source
               AND r.created_at > now() - interval '1 hour';
            IF v_n >= 3 THEN
                RAISE EXCEPTION 'file_gripe_readonly: run cap — agent:% filed % gripes in the last hour (max 3)', p_source, v_n;
            END IF;
        END IF;
        SELECT count(*) INTO v_n
          FROM refs r
          JOIN ref_tags rt ON rt.ref_id = r.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE r.kind = 'gripe'
           AND t.namespace = 'OPEN'
           AND t.value = 'source:agent_ro'
           AND r.created_at > now() - interval '1 hour';
        IF v_n >= 20 THEN
            RAISE EXCEPTION 'file_gripe_readonly: hourly cap — % agent_ro-sourced gripes in the last hour (max 20)', v_n;
        END IF;
    END IF;

    INSERT INTO refs (kind, title, meta)
    VALUES ('gripe', p_text, '{}'::jsonb)
    RETURNING ref_id INTO v_ref_id;

    INSERT INTO chunks (ref_id, ord, chunk_kind, text)
    VALUES (v_ref_id, 0, 'gripe_body', p_text);

    -- STATUS:open always; the ro stamps only on the ro-sourced path.
    FOR v_ns, v_val IN
        SELECT ns, val FROM (VALUES ('STATUS', 'open', true),
                              ('OPEN', 'source:agent_ro', v_ro OR p_source IS NOT NULL),
                              ('OPEN', 'friction', v_ro OR p_source IS NOT NULL),
                              ('OPEN', 'agent:' || p_source, p_source IS NOT NULL))
                 AS s(ns, val, wanted)
         WHERE wanted
    LOOP
        -- Read first: no row lock on the shared tag row (gr537784).
        SELECT tag_id INTO v_tag_id FROM tags WHERE namespace = v_ns AND value = v_val;
        IF v_tag_id IS NULL THEN
            INSERT INTO tags (namespace, value) VALUES (v_ns, v_val)
                ON CONFLICT (namespace, value) DO NOTHING
                RETURNING tag_id INTO v_tag_id;
            IF v_tag_id IS NULL THEN
                SELECT tag_id INTO v_tag_id FROM tags WHERE namespace = v_ns AND value = v_val;
            END IF;
        END IF;
        INSERT INTO ref_tags (ref_id, tag_id, set_by) VALUES (v_ref_id, v_tag_id, 'agent');
    END LOOP;

    RETURN v_ref_id;
END
$$;

COMMENT ON FUNCTION public.file_gripe_readonly(text, text) IS
    'Insert exactly one gripe (ref + gripe_body chunk + STATUS:open tag) and '
    'nothing else. SECURITY DEFINER so an agent_ro connection (write:none '
    'envelope) can still file a gripe; see envelope.py + handlers/gripe.py. '
    'ro-sourced filings (p_source given, or the effective role — outer '
    'SET ROLE else session_user — cannot INSERT refs) are stamped '
    'source:agent_ro + friction + agent:<p_source> inside the definer and '
    'bounded under an advisory lock: 4000 chars, 3 per agent tag/hour, 20 '
    'ro-sourced gripes/hour (0244, gr516603). Tag ids are read first, never '
    'upserted with DO UPDATE — no row lock on shared tag rows (0265, gr537784).';

REVOKE ALL ON FUNCTION public.file_gripe_readonly(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.file_gripe_readonly(text, text) TO PUBLIC;

COMMIT;
