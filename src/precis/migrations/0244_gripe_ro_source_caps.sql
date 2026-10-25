-- 0244_gripe_ro_source_caps.sql
--
-- ro-gripes (gr516603, spec shipped as docs/backlog/ro-gripes.md): extend
-- 0079's ``file_gripe_readonly`` so a read-only run's gripe is distinguishable
-- and bounded. The function gains ``p_source`` — the run's agent label — and,
-- whenever the filing is ro-sourced, stamps the tags itself (inside the
-- definer, where the INSERT grants exist: an ``agent_ro`` caller holds no
-- ``ref_tags`` grant, so the handler could never tag its own filing):
--
--   * ``source:agent_ro``  — triage batches on this
--   * ``friction``         — the channel exists for tool friction
--   * ``agent:<p_source>`` — the run label, validated [a-z0-9_.:-]{1,48}
--
-- Bounds enforced in SQL (a client bug or a prompt-injected ro agent cannot
-- bypass them): 4000 chars per body, 3 gripes per ``agent:<p_source>`` tag in
-- the last hour (the per-run cap — the label is caller-supplied and so
-- spoofable), and 20 ``source:agent_ro`` gripes per hour total (the per-role
-- backstop — the real bound). The count-then-insert is serialized on one
-- transaction-scoped advisory lock so concurrent runs can't both count
-- below the cap and both overshoot under READ COMMITTED.
--
-- "ro-sourced" is decided two ways: the caller asked for a stamp
-- (``p_source IS NOT NULL`` — the handler passes the run's label whenever
-- its run is envelope-denied or its connection is read-only, and an rw
-- consumer relaying a DB-less agent's FRICTION block passes it too), OR the
-- *effective* role itself cannot INSERT refs (checked rather than trusted
-- so a ro caller passing p_source = NULL is still stamped and capped, not
-- silently unbounded). "Effective" covers both tier-2 mechanisms:
-- ``SET ROLE`` moves ``current_setting('role')`` — which still reports the
-- OUTER role inside this SECURITY DEFINER body (``current_user`` is the
-- definer owner, ``session_user`` the login) — while a role-level
-- connection moves ``session_user``.
--
-- Signature change: an OVERLOAD, not a replace. 0079's one-arg function is
-- never dropped — the ship gate bans DROP FUNCTION, and a dropped-then-
-- recreated signature breaks any caller still resolved to the old oid. The
-- stamp+cap logic lives in a NEW two-arg overload whose ``p_source`` takes
-- NO default — a default would make a one-arg call ambiguous between the
-- two overloads. The one-arg is then CREATE OR REPLACE'd into a thin
-- wrapper that delegates with p_source = NULL, so the existing one-arg
-- calls — ``GripeHandler._insert_gripe`` on the ordinary write path —
-- keep working unchanged; a genuinely ro caller through the wrapper is
-- still stamped and capped by the effective-role probe inside.
--
-- Forward-only (ADR 0005). Idempotent (CREATE OR REPLACE).

BEGIN;

CREATE OR REPLACE FUNCTION public.file_gripe_readonly(p_text text, p_source text)
    RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_ref_id bigint;
    v_tag_id bigint;
    v_ro boolean;
    v_role text;
    v_n integer;
BEGIN
    IF p_text IS NULL OR length(btrim(p_text)) = 0 THEN
        RAISE EXCEPTION 'file_gripe_readonly: text must not be empty';
    END IF;
    IF p_source IS NOT NULL AND p_source !~ '^[a-z0-9_.:-]{1,48}$' THEN
        RAISE EXCEPTION 'file_gripe_readonly: source label % must match [a-z0-9_.:-]{1,48}', p_source;
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
    VALUES ('gripe', p_text, '{}'::jsonb)
    RETURNING ref_id INTO v_ref_id;

    INSERT INTO chunks (ref_id, ord, chunk_kind, text)
    VALUES (v_ref_id, 0, 'gripe_body', p_text);

    -- Mirrors GripeHandler.default_tags_on_create = ("STATUS:open",).
    INSERT INTO tags (namespace, value) VALUES ('STATUS', 'open')
        ON CONFLICT (namespace, value) DO UPDATE SET namespace = EXCLUDED.namespace
        RETURNING tag_id INTO v_tag_id;
    INSERT INTO ref_tags (ref_id, tag_id, set_by) VALUES (v_ref_id, v_tag_id, 'agent');

    IF v_ro OR p_source IS NOT NULL THEN
        -- Stamped inside the definer: the ro caller holds no ref_tags grant.
        -- Lowercase ns:value tags live in the OPEN namespace (the tags-table
        -- convention — store/_tags_ops.py).
        INSERT INTO tags (namespace, value) VALUES ('OPEN', 'source:agent_ro')
            ON CONFLICT (namespace, value) DO UPDATE SET namespace = EXCLUDED.namespace
            RETURNING tag_id INTO v_tag_id;
        INSERT INTO ref_tags (ref_id, tag_id, set_by) VALUES (v_ref_id, v_tag_id, 'agent');

        INSERT INTO tags (namespace, value) VALUES ('OPEN', 'friction')
            ON CONFLICT (namespace, value) DO UPDATE SET namespace = EXCLUDED.namespace
            RETURNING tag_id INTO v_tag_id;
        INSERT INTO ref_tags (ref_id, tag_id, set_by) VALUES (v_ref_id, v_tag_id, 'agent');

        IF p_source IS NOT NULL THEN
            INSERT INTO tags (namespace, value) VALUES ('OPEN', 'agent:' || p_source)
                ON CONFLICT (namespace, value) DO UPDATE SET namespace = EXCLUDED.namespace
                RETURNING tag_id INTO v_tag_id;
            INSERT INTO ref_tags (ref_id, tag_id, set_by) VALUES (v_ref_id, v_tag_id, 'agent');
        END IF;
    END IF;

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
    'ro-sourced gripes/hour (0244, gr516603).';

REVOKE ALL ON FUNCTION public.file_gripe_readonly(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.file_gripe_readonly(text, text) TO PUBLIC;

-- 0079's one-arg signature stays live as a thin wrapper over the overload
-- above (never dropped — see the header). p_source = NULL means "no run
-- label": the effective-role probe inside still stamps+caps a genuinely
-- ro caller, while an rw caller files un-stamped and un-capped exactly as
-- 0079 did.
CREATE OR REPLACE FUNCTION public.file_gripe_readonly(p_text text)
    RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    RETURN public.file_gripe_readonly(p_text, NULL);
END
$$;

COMMENT ON FUNCTION public.file_gripe_readonly(text) IS
    'Insert exactly one gripe (ref + gripe_body chunk + STATUS:open tag) and '
    'nothing else. SECURITY DEFINER so an agent_ro connection (write:none '
    'envelope) can still file a gripe; see envelope.py + handlers/gripe.py. '
    'One-arg wrapper over file_gripe_readonly(text, text) with p_source NULL '
    '(0244): a ro effective role is still stamped + capped inside.';

REVOKE ALL ON FUNCTION public.file_gripe_readonly(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.file_gripe_readonly(text) TO PUBLIC;

COMMIT;
