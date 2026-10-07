-- R14: local successor staging; never rewrite signed artifacts or OTS proofs.
-- A discarded unsigned candidate leaves a pending obligation, reattached by
-- nanopub_create_publish_row. This is separate from disposable grounding JSON.
CREATE TABLE IF NOT EXISTS nanopub_supersessions (
    predecessor_id BIGINT PRIMARY KEY REFERENCES nanopub_publish(id),
    successor_id BIGINT UNIQUE REFERENCES nanopub_publish(id) ON DELETE SET NULL,
    CONSTRAINT nanopub_supersessions_distinct CHECK (successor_id <> predecessor_id)
);
COMMENT ON TABLE nanopub_supersessions IS
    'Successor supersedes predecessor; NULL successor retains the obligation after candidate discard until restaging.';

-- Durable 'publishing' claim: the registry POST runs with NO database
-- transaction open. A short transaction CAS-flips anchored -> publishing
-- (stamping publishing_since and the target registry_url) and commits; the
-- POST follows; a second short transaction flips publishing -> published, or
-- reverts to anchored on a definite failure. An ambiguous outcome (timeout,
-- dropped connection) leaves the row 'publishing' for
-- `precis nanopub reconcile`, which asks the registry for the (pre-known)
-- trusty URI. 'publishing' is a live (non-terminal) state, so the partial
-- unique index keeps excluding exactly superseded/retracted/rejected.
ALTER TABLE nanopub_publish
    ADD COLUMN IF NOT EXISTS publishing_since TIMESTAMPTZ;
ALTER TABLE nanopub_publish
    DROP CONSTRAINT IF EXISTS nanopub_publish_state_check;
ALTER TABLE nanopub_publish
    ADD CONSTRAINT nanopub_publish_state_check
    CHECK (state IN ('candidate', 'reviewed', 'signed', 'anchored',
                     'publishing', 'published', 'superseded', 'retracted',
                     'rejected'));
COMMENT ON COLUMN nanopub_publish.publishing_since IS
    'Claim time of the in-flight registry POST (state=publishing); NULL otherwise. Stuck rows are those with an old value.';
