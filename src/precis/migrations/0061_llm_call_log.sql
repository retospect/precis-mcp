-- Full LLM interaction log (Slice 1 of the adaptive-router plan). Records every
-- dispatch() call's full logical request + final response + outcome metadata to
-- postgres, for later model comparison + routing eval. Content-addressed blobs
-- dedup the (huge, repeated) system prompts; the log row references them by hash.
-- Operational, NOT corpus: never embedded (peer to agentlog / alert). Dark: the
-- writer is best-effort and no-ops until a store is bound at worker boot.

-- Content-addressed blob store: each distinct prompt/response text stored once.
CREATE TABLE IF NOT EXISTS llm_blob (
    hash        TEXT        PRIMARY KEY,   -- sha256 hex of text
    text        TEXT        NOT NULL,
    bytes       INT         NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per dispatch() call: full I/O (via blob refs) + queryable metadata +
-- the immediate outcome (known at return; lagged outcomes join later via ref_id).
CREATE TABLE IF NOT EXISTS llm_call_log (
    id             BIGSERIAL   PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    source         TEXT,                     -- caller label (dream / review:structural / chase:verify / ...)
    tier           TEXT,
    transport      TEXT,
    model          TEXT,                     -- what actually ran
    tools_needed   BOOLEAN,
    request_hash   TEXT REFERENCES llm_blob(hash),   -- full serialized request
    response_hash  TEXT REFERENCES llm_blob(hash),   -- final response text
    request_chars  INT,
    response_chars INT,
    cost_usd       DOUBLE PRECISION,
    turns_used     INT,
    duration_ms    INT,
    errored        BOOLEAN     NOT NULL DEFAULT FALSE,
    error          TEXT,
    data_parsed    BOOLEAN,                  -- judge JSON populated? (an immediate quality signal)
    ref_id         BIGINT,                   -- correlation key for lagged outcomes
    features       JSONB                     -- extensible: extracted code features
);

CREATE INDEX IF NOT EXISTS llm_call_log_ts_idx ON llm_call_log (ts DESC);
CREATE INDEX IF NOT EXISTS llm_call_log_source_ts_idx ON llm_call_log (source, ts DESC);
CREATE INDEX IF NOT EXISTS llm_call_log_ref_idx ON llm_call_log (ref_id) WHERE ref_id IS NOT NULL;
