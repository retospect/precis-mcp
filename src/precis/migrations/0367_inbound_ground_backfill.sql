-- 0367_inbound_ground_backfill.sql
-- Pair verdicts commit with their evidence. Completion skips exhausted claims.

CREATE TABLE IF NOT EXISTS inbound_grounded (
    claim_ref_id bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    paper_ref_id bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    verdict text NOT NULL CHECK (verdict IN ('support', 'deny', 'no-relation')),
    PRIMARY KEY (claim_ref_id, paper_ref_id)
);

CREATE INDEX IF NOT EXISTS inbound_grounded_paper_idx ON inbound_grounded (paper_ref_id);

CREATE TABLE IF NOT EXISTS inbound_ground_backfilled (
    claim_ref_id bigint PRIMARY KEY REFERENCES refs(ref_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS inbound_ground_budget (
    day_utc date PRIMARY KEY,
    tokens bigint NOT NULL CHECK (tokens >= 0)
);
