-- OAuth state is private authorization data, never corpus refs/chunks.
CREATE TABLE IF NOT EXISTS mcp_oauth_requests (
    request_sha256 text PRIMARY KEY CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    client_id text NOT NULL,
    params jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    user_id bigint REFERENCES web_users(id) ON DELETE CASCADE,
    csrf_sha256 text CHECK (csrf_sha256 ~ '^[0-9a-f]{64}$'),
    consumed_at timestamptz
);
CREATE INDEX IF NOT EXISTS mcp_oauth_requests_user_idx ON mcp_oauth_requests (user_id);
CREATE INDEX IF NOT EXISTS mcp_oauth_requests_expiry_idx ON mcp_oauth_requests (expires_at);

CREATE TABLE IF NOT EXISTS mcp_oauth_grants (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    client_id text NOT NULL,
    label text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 100),
    scopes text[] NOT NULL CHECK (cardinality(scopes) > 0),
    resource text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS mcp_oauth_grants_user_idx ON mcp_oauth_grants (user_id);

CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
    code_sha256 text PRIMARY KEY CHECK (code_sha256 ~ '^[0-9a-f]{64}$'),
    grant_id bigint NOT NULL REFERENCES mcp_oauth_grants(id) ON DELETE CASCADE,
    code_challenge text NOT NULL,
    redirect_uri text NOT NULL,
    redirect_uri_explicit boolean NOT NULL,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz
);
CREATE INDEX IF NOT EXISTS mcp_oauth_codes_grant_idx ON mcp_oauth_codes (grant_id);

CREATE TABLE IF NOT EXISTS mcp_oauth_tokens (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_sha256 text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    grant_id bigint NOT NULL REFERENCES mcp_oauth_grants(id) ON DELETE CASCADE,
    token_kind text NOT NULL CHECK (token_kind IN ('access', 'refresh')),
    scopes text[] NOT NULL CHECK (cardinality(scopes) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz
);
CREATE INDEX IF NOT EXISTS mcp_oauth_tokens_grant_idx ON mcp_oauth_tokens (grant_id);
