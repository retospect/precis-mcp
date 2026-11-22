-- 0351_user_key_memory.sql

BEGIN;

ALTER TABLE web_users
    ADD COLUMN key_memory_id bigint,
    ADD COLUMN is_developer boolean NOT NULL DEFAULT false;

DO $$
DECLARE
    account_id bigint;
    memory_id bigint;
BEGIN
    FOR account_id IN SELECT id FROM web_users LOOP
        INSERT INTO refs (kind, title, owner_login)
        SELECT 'memory', 'Key memory', login FROM web_users WHERE id = account_id
        RETURNING ref_id INTO memory_id;

        UPDATE web_users SET key_memory_id = memory_id WHERE id = account_id;
    END LOOP;

    -- The role exists to onboard non-developers; every account that
    -- predates it is a developer and keeps the full startup text.
    UPDATE web_users SET is_developer = true;
END;
$$;

COMMENT ON COLUMN web_users.key_memory_id IS
    'Ref id of the account''s key-memory hub, minted as a stub at insert and shared by every grant and token.';
COMMENT ON COLUMN web_users.is_developer IS
    'Developer accounts get maintainer machinery (gripes, backlog, status) in MCP startup text; plain accounts get the reduced text.';

ALTER TABLE web_users ALTER COLUMN key_memory_id SET DEFAULT nextval('refs_ref_id_seq');

-- The small account roster is already locked for the atomic backfill.
-- squawk-ignore adding-not-nullable-field
ALTER TABLE web_users ALTER COLUMN key_memory_id SET NOT NULL;

-- squawk-ignore require-concurrent-index-creation
CREATE UNIQUE INDEX web_users_key_memory_unique ON web_users (key_memory_id);

ALTER TABLE web_users
    ADD CONSTRAINT web_users_key_memory_fkey
        FOREIGN KEY (key_memory_id) REFERENCES refs (ref_id)
        DEFERRABLE INITIALLY DEFERRED NOT VALID;

-- squawk-ignore constraint-missing-not-valid
ALTER TABLE web_users VALIDATE CONSTRAINT web_users_key_memory_fkey;

-- Reserve the id with the account's default and mint only after insertion.
-- Failed inserts and ON CONFLICT DO NOTHING cannot leave orphan stubs.
CREATE FUNCTION mint_web_user_key_memory() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO refs (ref_id, kind, title, owner_login)
    VALUES (NEW.key_memory_id, 'memory', 'Key memory', NEW.login);
    RETURN NEW;
END;
$$;

CREATE TRIGGER web_users_key_memory
    AFTER INSERT ON web_users
    FOR EACH ROW EXECUTE FUNCTION mint_web_user_key_memory();

DO $$
BEGIN
    IF to_regrole('agent_rw') IS NOT NULL THEN
        GRANT SELECT (key_memory_id, is_developer) ON web_users TO agent_rw;
    END IF;
    IF to_regrole('agent_ro') IS NOT NULL THEN
        GRANT SELECT (key_memory_id, is_developer) ON web_users TO agent_ro;
    END IF;
END;
$$;

COMMIT;
