-- 0196_web_users_discord_user_id.sql
--
-- The account's numeric Discord user id (a snowflake). asa_bot sees a Discord
-- message author only as a display name; this column is how it learns which
-- precis account that person is, so its writes carry the account's `abbrev`
-- instead of a mutable display name and per-user state keys off a stable id.
--
-- Nullable; partial unique index — one Discord user maps to at most one
-- account, and "nobody has one" stays unconstrained. Digits only, 17-20 long
-- (snowflakes are 64-bit; current ones are 18-19 digits). Never the display
-- name or `name#discriminator`: those change, ids do not.
--
-- Forward-only (ADR 0005). Idempotent.

ALTER TABLE web_users
    ADD COLUMN IF NOT EXISTS discord_user_id text
    CHECK (discord_user_id IS NULL OR discord_user_id ~ '^[0-9]{17,20}$');

CREATE UNIQUE INDEX IF NOT EXISTS web_users_discord_user_id_key
    ON web_users (discord_user_id) WHERE discord_user_id IS NOT NULL;

COMMENT ON COLUMN web_users.discord_user_id IS
    'Numeric Discord user id (snowflake). Lets asa_bot attribute a Discord speaker to this account.';
