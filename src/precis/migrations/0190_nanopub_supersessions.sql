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
