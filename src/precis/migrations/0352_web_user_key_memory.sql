-- 0352_web_user_key_memory.sql
--
-- Break the refs <-> web_users FK cycle 0351 introduced (td761883).
-- refs.owner_login already references web_users(login) (0164); 0351's
-- web_users.key_memory_id -> refs(ref_id) closed the loop, which
-- tests/test_schema_design.py::test_no_new_fk_cycles rejects.
--
-- The pointer moves to a join table keyed by web_users.id, the shape of
-- the other per-user ref pointers (0220 web_ref_views/web_collections):
--
--   * web_user_key_memory — one row per account: its key-memory hub.
--     user_id cascades with the account; ref_id is UNIQUE (one hub per
--     account) and NO ACTION, so a hub cannot be hard-deleted while its
--     account points at it — the same guard 0351's FK gave.
--
-- The insert trigger keeps minting the hub after the account row lands,
-- so failed inserts and ON CONFLICT DO NOTHING still leave no orphan
-- stub. Writes follow the web_users roster: agent_rw reads only,
-- precis_ingress (the web daemon, the roster writer) writes.
--
-- Forward-only (ADR 0005); 0351 stays sealed.

BEGIN;

CREATE TABLE web_user_key_memory (
    user_id bigint NOT NULL PRIMARY KEY
                   REFERENCES web_users (id) ON DELETE CASCADE,
    ref_id  bigint NOT NULL UNIQUE REFERENCES refs (ref_id)
);

COMMENT ON TABLE web_user_key_memory IS
    'Each account''s key-memory hub: one ref per web user, minted as a stub '
    'by the web_users insert trigger and shared by every grant and token.';

INSERT INTO web_user_key_memory (user_id, ref_id)
SELECT id, key_memory_id FROM web_users;

CREATE OR REPLACE FUNCTION mint_web_user_key_memory() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    memory_id bigint;
BEGIN
    INSERT INTO refs (kind, title, owner_login)
    VALUES ('memory', 'Key memory', NEW.login)
    RETURNING ref_id INTO memory_id;
    INSERT INTO web_user_key_memory (user_id, ref_id)
    VALUES (NEW.id, memory_id);
    RETURN NEW;
END;
$$;

-- 0351's FK never shipped; the guarantee moves to web_user_key_memory.
-- squawk-ignore ban-drop-constraint
ALTER TABLE web_users DROP CONSTRAINT web_users_key_memory_fkey;
DROP INDEX web_users_key_memory_unique;
-- Unreleased column (0351 never reached prod); no reader survives this commit.
-- squawk-ignore ban-drop-column
ALTER TABLE web_users DROP COLUMN key_memory_id;

DO $$
BEGIN
    IF to_regrole('agent_rw') IS NOT NULL THEN
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE
            ON web_user_key_memory FROM agent_rw;
        GRANT SELECT ON web_user_key_memory TO agent_rw;
    END IF;
    IF to_regrole('agent_ro') IS NOT NULL THEN
        GRANT SELECT ON web_user_key_memory TO agent_ro;
    END IF;
    IF to_regrole('precis_ingress') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON web_user_key_memory TO precis_ingress;
    END IF;
END;
$$;

COMMIT;
