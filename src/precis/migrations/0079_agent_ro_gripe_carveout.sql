-- 0079_agent_ro_gripe_carveout.sql
--
-- agent_ro gripe-write carve-out (Reto's explicit decision, OPEN-ITEMS.md /
-- docs/design/... ): ANY write:none task (mapped by envelope.py::db_role to
-- the read-only ``agent_ro`` role) should still be able to file a gripe when
-- it hits tool friction — not just write-capable tasks. Today Postgres
-- itself refuses every INSERT for ``agent_ro``, including the one gripe
-- INSERT we actually want to let through.
--
-- A SECURITY DEFINER function runs with its OWNER's privileges regardless of
-- the calling role, so it can do the one narrow write (a "gripe" ref + its
-- body chunk + a STATUS:open tag — exactly what
-- ``handlers/gripe.py::GripeHandler._create`` does on the normal write path)
-- even from an agent_ro connection that holds no direct INSERT grant on
-- refs/chunks/tags/ref_tags.
--
-- Mirrors the vault pattern (0059_secrets_vault.sql): a narrow, named,
-- SECURITY DEFINER verb. Granted to PUBLIC rather than to a named
-- ``agent_ro`` role, for the same reason vault's functions are: v1 trust
-- model is "the function itself is the boundary, not a role grant" — and
-- ``agent_ro``/``agent_rw`` are provisioned out-of-tree in ansible, not by
-- an in-repo migration, so a fresh/test DB has no such role to grant to.
--
-- Forward-only (ADR 0005). Idempotent (CREATE OR REPLACE).

BEGIN;

CREATE OR REPLACE FUNCTION public.file_gripe_readonly(p_text text)
    RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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

COMMENT ON FUNCTION public.file_gripe_readonly(text) IS
    'Insert exactly one gripe (ref + gripe_body chunk + STATUS:open tag) and '
    'nothing else. SECURITY DEFINER so an agent_ro connection (write:none '
    'envelope) can still file a gripe; see envelope.py + handlers/gripe.py.';

REVOKE ALL ON FUNCTION public.file_gripe_readonly(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.file_gripe_readonly(text) TO PUBLIC;

COMMIT;
