-- 0368_gripe_status_held_friction.sql
--
-- Friction-gripe holding lane (gr764123): a friction gripe — one filed
-- through the ``PRECIS_FRICTION_REFLECT`` footer's channel — no longer
-- enters the open queue at mint. It lands
-- at ``STATUS:held``: non-terminal, off every open queue (``/open``,
-- the ``status='open'`` live cohort, the attention badge, the fix
-- prefilters — the parked shelf's exclusions), still searchable
-- (``status='held'`` / ``status='*'``). The ``gripe_triage`` tick's
-- friction sweep then groups and dedups held filings: a group releases
-- to ``STATUS:open`` once reports from 3 separate runs land within 30
-- days, else it ages out to ``wontfix`` quietly.
--
-- Two forward-only function replacements, preserving the read-first
-- tag resolution from 0265/0320 (the ship gate
-- bans DROP FUNCTION; both are CREATE OR REPLACE):
--
--   * ``gripe_status_check()`` gains 'held' — the thirteen-value list;
--     the three 0176 constraint triggers call it by name, untouched.
--     Existing rows are all in-vocabulary already, so no backfill.
--   * ``file_gripe_readonly(text, text, text)`` writes ``STATUS:held``
--     for ro-sourced text starting with 'friction: '. Ordinary stamped
--     filings retain their original tags, caps and STATUS:open. The
--     one- and two-arg wrappers delegate unchanged. The handler holds
--     write-capable friction-tagged reports in its create transaction.

BEGIN;

SET LOCAL lock_timeout = '5s';

INSERT INTO tags (namespace, value) VALUES ('STATUS', 'held')
    ON CONFLICT (namespace, value) DO NOTHING;

CREATE OR REPLACE FUNCTION public.gripe_status_check() RETURNS trigger
    LANGUAGE plpgsql AS $$
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
               ('open', 'triaged', 'fixing', 'ready_for_fix', 'in_review',
                'verify', 'auto_verify', 'user_verify', 'needs_spec',
                'parked', 'held', 'done', 'wontfix'))
      INTO v_n, v_bad
      FROM ref_tags rt
      JOIN tags t ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
     WHERE rt.ref_id = v_ref_id;

    IF v_n <> 1 OR v_bad > 0 THEN
        RAISE EXCEPTION
            'gripe % must have exactly one STATUS tag from '
            '(open, triaged, fixing, ready_for_fix, in_review, verify, '
            'auto_verify, user_verify, needs_spec, parked, held, done, '
            'wontfix); '
            'found % STATUS tag(s), % off-vocabulary',
            v_ref_id, v_n, v_bad
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION public.file_gripe_readonly(
    p_text text, p_source text, p_idem_key text
)
    RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_ref_id bigint;
    v_tag_id bigint;
    v_ro boolean;
    v_footer_friction boolean;
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

    v_footer_friction := (v_ro OR p_source IS NOT NULL)
        AND starts_with(p_text, 'friction: ');

    INSERT INTO refs (kind, title, meta)
    VALUES (
        'gripe',
        p_text,
        jsonb_strip_nulls(jsonb_build_object(
            'idem_key', p_idem_key,
            'footer_friction', CASE WHEN v_footer_friction THEN true END
        ))
    )
    RETURNING ref_id INTO v_ref_id;

    INSERT INTO chunks (ref_id, ord, chunk_kind, text)
    VALUES (v_ref_id, 0, 'gripe_body', p_text);

    -- Only marked footer filings enter the grouping lane at mint.
    -- Lowercase ns:value tags live in the OPEN namespace (the tags-table
    -- convention — store/_tags_ops.py). Stamped inside the definer: the ro
    -- caller holds no ref_tags grant.
    FOR v_ns, v_val IN
        SELECT ns, val FROM (VALUES ('STATUS', CASE WHEN v_footer_friction THEN 'held' ELSE 'open' END, true),
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
    'Insert exactly one gripe (ref + gripe_body chunk + a STATUS tag) and '
    'nothing else. SECURITY DEFINER so an agent_ro connection (write:none '
    'envelope) can still file a gripe; see envelope.py + handlers/gripe.py. '
    'ro-sourced filings (p_source given, or the effective role — outer '
    'SET ROLE else session_user — cannot INSERT refs) are stamped '
    'source:agent_ro + friction + agent:<p_source> inside the definer and '
    'bounded under an advisory lock: 4000 chars, 3 per agent tag/hour, 20 '
    'ro-sourced gripes/hour (0244, gr516603). A stamped filing starting '
    'with friction: (including the trailing space) lands STATUS:held '
    'and meta.footer_friction for grouping (gr764123). Other filings land '
    'STATUS:open. p_idem_key (0319, gr658432) is recorded on meta.idem_key '
    'at mint — the dedupe stamp a no-UPDATE role could never write after '
    'the fact. Tag ids are resolved read-first to avoid exclusive locks '
    'on shared tag rows (0265, 0320).';

REVOKE ALL ON FUNCTION public.file_gripe_readonly(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.file_gripe_readonly(text, text, text) TO PUBLIC;

COMMIT;
