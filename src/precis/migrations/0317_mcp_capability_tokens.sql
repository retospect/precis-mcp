-- 0317_mcp_capability_tokens.sql
-- Client capability tokens (gr595554): a third bearer class beside the
-- install token and the personal token. Same table, same digest-only
-- storage, same revoke path; the class and a per-token allow-list ride
-- as columns. A 'personal' row keeps NULL allow-lists and the
-- learning-only gate; a 'capability' row names a consumer (a chat
-- connector, a podcast app) and carries its own verbs and kinds.
ALTER TABLE web_user_mcp_tokens
    ADD COLUMN IF NOT EXISTS token_class text NOT NULL DEFAULT 'personal';
ALTER TABLE web_user_mcp_tokens
    ADD COLUMN IF NOT EXISTS allow_verbs text[];
-- NULL on a capability row = every non-private kind, read verbs only.
ALTER TABLE web_user_mcp_tokens
    ADD COLUMN IF NOT EXISTS allow_kinds text[];
-- True admits the token as the podcast feed credential (?t=).
ALTER TABLE web_user_mcp_tokens
    ADD COLUMN IF NOT EXISTS feed_access boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN web_user_mcp_tokens.token_class IS
    'personal (learning-only, names a person) or capability (names a consumer; allow_verbs/allow_kinds/feed_access apply).';
