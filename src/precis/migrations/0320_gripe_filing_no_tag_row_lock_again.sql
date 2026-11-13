-- 0320_gripe_filing_no_tag_row_lock_again.sql
--
-- 0319 regressed 0265 (gr537784): its three-arg ``file_gripe_readonly``
-- copied 0244's tag resolution — ``INSERT ... ON CONFLICT DO UPDATE SET
-- namespace = EXCLUDED.namespace`` — which row-locks the shared
-- ``STATUS:open`` tag row to commit, so concurrent filings block on
-- lock_timeout again (tests/test_gripe_lock_retry.py).
--
-- Same body as 0319's three-arg overload, with 0265's read-first tag
-- resolution restored. Signature unchanged; the two-arg wrapper delegates
-- here and needs no change.
--
-- Forward-only (ADR 0005). Idempotent (CREATE OR REPLACE).

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE FUNCTION public.file_gripe_readonly(
    p_text text, p_source text, p_idem_key text
)
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
    IF p_idem_key IS NOT NULL AND (length(btrim(p_idem_key)) = 0 OR length(p_idem_key) > 200) THEN
        RAISE EXCEPTION 'file_gripe_readonly: idem_key must be 1-200 chars (got %)', length(p_idem_key);
    END IF;

    -- Effective-role capability, not the arg: a ro caller passing
    -- p_source = NULL takes the stamped+capped path anyway (no cap
    -- bypass). ``current_setting('role')`` names the outer SET ROLE
    -- (tier-2's mechanism — it still reports the session's role inside
    -- this SECURITY DEFINER body, while ``current_user`` here is the
    -- definer owner); fall back to ``session_user`` for a login-level
    -- ro role (or SET SESSION AUTHORIZATION).
    v_role := COALESCE(NULLIF(current_setting('role'), 'none'), session_user);
    v_ro := NOT has_table_privilege(v_role, 'refs', 'INSERT');

    IF v_ro OR p_source IS NOT NULL THEN
        IF length(p_text) > 4000 THEN
            RAISE EXCEPTION 'file_gripe_readonly: text exceeds the 4000-char limit (got % chars)', length(p_text);
        END IF;
        -- Serialize count-then-insert: under READ COMMITTED two
        -- concurrent runs could each count below the cap and both
        -- insert. One xact-scoped advisory lock per function is enough
        -- (this channel is low-throughput by design).
        PERFORM pg_advisory_xact_lock(hashtext('file_gripe_readonly'));
        -- Per-run cap: 3 gripes per agent:<source> tag in the last hour.
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
        -- Per-role backstop: 20 agent_ro-sourced gripes per hour, however the
        -- sources slice themselves.
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
    VALUES (
        'gripe',
        p_text,
        CASE WHEN p_idem_key IS NULL
             THEN '{}'::jsonb
             ELSE jsonb_build_object('idem_key', p_idem_key)
        END
    )
    RETURNING ref_id INTO v_ref_id;

    INSERT INTO chunks (ref_id, ord, chunk_kind, text)
    VALUES (v_ref_id, 0, 'gripe_body', p_text);

    -- Mirrors GripeHandler.default_tags_on_create = ("STATUS:open",).
    -- Lowercase ns:value tags live in the OPEN namespace (the tags-table
    -- convention — store/_tags_ops.py). Stamped inside the definer: the ro
    -- caller holds no ref_tags grant.
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

COMMENT ON FUNCTION public.file_gripe_readonly(text, text, text) IS
    'Insert exactly one gripe (ref + gripe_body chunk + STATUS:open tag) and '
    'nothing else. SECURITY DEFINER so an agent_ro connection (write:none '
    'envelope) can still file a gripe; see envelope.py + handlers/gripe.py. '
    'ro-sourced filings (p_source given, or the effective role — outer '
    'SET ROLE else session_user — cannot INSERT refs) are stamped '
    'source:agent_ro + friction + agent:<p_source> inside the definer and '
    'bounded under an advisory lock: 4000 chars, 3 per agent tag/hour, 20 '
    'ro-sourced gripes/hour (0244, gr516603). p_idem_key (0319, gr658432) '
    'is recorded on meta.idem_key at mint — the dedupe stamp a no-UPDATE '
    'role could never write after the fact. Tag ids are read first, never '
    'row-locked (0265, 0320).';

REVOKE ALL ON FUNCTION public.file_gripe_readonly(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.file_gripe_readonly(text, text, text) TO PUBLIC;

COMMIT;
