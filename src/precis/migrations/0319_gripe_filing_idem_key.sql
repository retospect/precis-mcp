-- 0319_gripe_filing_idem_key.sql
--
-- Keyed gripe filings under a no-UPDATE role (gr658432, td659660 review).
--
-- ``put(idem_key=…)`` stamps ``meta.idem_key`` on the minted ref so a blind
-- retry dedupes to it (``_run_idempotent`` in handlers/_numeric_ref.py).
-- The stamp used to be a post-hoc ``UPDATE refs`` — fine for ``agent_rw``,
-- but a read-scoped filing (``write:none`` envelope, ``agent_ro``) holds no
-- UPDATE grant on ``refs``: the stamp raised, the keyed scope rolled back,
-- and the filing wrote nothing at all.
--
-- The fix moves the stamp inside the one write boundary such a caller CAN
-- cross: this SECURITY DEFINER function. A new ``p_idem_key`` parameter is
-- recorded on the ref's ``meta`` at INSERT, before the caller's role can
-- refuse anything. The handler's post-hoc UPDATE is skipped whenever the
-- mint already carries the key (``idem.key_on_ref``), so a keyed filing
-- under ``agent_ro`` files once, stamped, and a same-key retry replays it
-- from the ordinary ``meta->>'idem_key'`` lookup.
--
-- Signature change: an OVERLOAD, not a replace — same rule as 0244 (the
-- ship gate bans DROP FUNCTION, and a dropped-then-recreated signature
-- breaks callers resolved to the old oid). The stamp+cap+insert logic
-- moves to a NEW three-arg overload whose ``p_idem_key`` takes NO default
-- — a default would make a two-arg call ambiguous between the overloads.
-- The two-arg is CREATE OR REPLACE'd into a thin wrapper delegating with
-- p_idem_key = NULL, so existing two-arg callers keep working unchanged;
-- 0079/0244's one-arg wrapper is untouched (it still resolves to the
-- two-arg).
--
-- Key validation mirrors ``utils/idem.MAX_KEY_LEN`` (200): longer is a
-- pasted payload, not a key; NULL = unkeyed filing, byte-identical to 0244.
--
-- Forward-only (ADR 0005). Idempotent (CREATE OR REPLACE).

BEGIN;

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
    'role could never write after the fact.';

REVOKE ALL ON FUNCTION public.file_gripe_readonly(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.file_gripe_readonly(text, text, text) TO PUBLIC;

-- 0244's two-arg signature stays live as a thin wrapper over the overload
-- above (never dropped — see the header). p_idem_key = NULL means "unkeyed
-- filing": byte-identical to 0244 for every existing caller.
CREATE OR REPLACE FUNCTION public.file_gripe_readonly(p_text text, p_source text)
    RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    RETURN public.file_gripe_readonly(p_text, p_source, NULL);
END
$$;

COMMENT ON FUNCTION public.file_gripe_readonly(text, text) IS
    'Insert exactly one gripe (ref + gripe_body chunk + STATUS:open tag) and '
    'nothing else. SECURITY DEFINER so an agent_ro connection (write:none '
    'envelope) can still file a gripe; see envelope.py + handlers/gripe.py. '
    'Two-arg wrapper over file_gripe_readonly(text, text, text) with '
    'p_idem_key NULL (0319): unkeyed filing, stamped + capped inside '
    'exactly as 0244 wrote it.';

REVOKE ALL ON FUNCTION public.file_gripe_readonly(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.file_gripe_readonly(text, text) TO PUBLIC;

COMMIT;
