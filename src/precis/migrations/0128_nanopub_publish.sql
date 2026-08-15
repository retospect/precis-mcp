-- 0128_nanopub_publish.sql
--
-- Nanopub publication state + append-only proof store
-- (docs/backlog/claim-publication-nanopub-ots.md, slices 2-3).
--
-- One schema family, designed together per the spec's migration section:
-- publish row -> artifact -> batch/leaf/proof rows.
--
--   * `nanopub_publish` — the ONE mutable working row per (hub, published
--     identity): the frozen approved claim string, its `claim_sha` /
--     AIDA URI, frozen grounding, and the state machine
--     candidate -> reviewed -> signed -> anchored -> published ->
--     superseded/retracted (`rejected` branches off `reviewed`). At most
--     one non-terminal row per hub (partial unique index). Content hashes
--     are never referents — `claim_ref_id` is the referent; `claim_sha`
--     here is the drift/staleness gate (third instance of the
--     claim_embeddings / hub_refine pattern).
--
--   * `nanopub_artifacts` — APPEND-ONLY. The exact serialized signed TriG
--     bytes (the authority) + indexed extracts (trusty/AIDA URI, signer,
--     DOIs — deliberately non-DRY; rebuildable, audited against the bytes
--     by the recompute audit; on mismatch the bytes win). `byte_sha256` is
--     GENERATED ALWAYS from the stored bytes — not writable, never wrong —
--     and is also the OTS leaf digest.
--
--   * `nanopub_ots_batches` / `nanopub_ots_leaves` / `nanopub_ots_proofs` —
--     APPEND-ONLY. A Merkle batch = one root + construction rule, one leaf
--     row per anchored artifact (leaf hash, index, serialized leaf->root
--     inclusion path — the leaf table IS part of the proof), and one proof
--     row per calendar state (`pending` at stamp; the upgrade sweep INSERTs
--     an `upgraded` row rather than updating, so pending history is kept).
--     A re-stamped artifact appears in a later batch; artifact_id is NOT
--     unique across leaves.
--
-- Append-only is a DB property, not a convention: a BEFORE UPDATE OR DELETE
-- trigger raises on every proof-store table (same guarantee the spec's
-- proof-store section requires; matches the `chunks` body-row convention,
-- here enforced). Corrections are new rows / new batches.
--
-- Forward-only (ADR 0005): additive, no data migration.

BEGIN;

CREATE OR REPLACE FUNCTION nanopub_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'nanopub table % is append-only (spec: proof store must '
        'be immutable and complete); corrections are new rows', TG_TABLE_NAME;
END $$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS nanopub_publish (
    id               BIGSERIAL   PRIMARY KEY,
    claim_ref_id     BIGINT      NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    artifact_type    TEXT        NOT NULL DEFAULT 'claim'
        CHECK (artifact_type IN ('claim', 'compound', 'hypothesis')),
    -- Frozen at review (freeze-at-review): the exact string the signature
    -- will cover. NULL while `candidate`.
    approved_title   TEXT,
    -- canon.claim_sha(approved_title) at approval — the drift gate.
    claim_sha        TEXT,
    -- Derived from approved_title, encoding canonicalised.
    aida_uri         TEXT,
    -- Frozen payload envelope, validated at mint: {"passages": [{doi,
    -- pdf_sha256, quote, snip, chunk_id, section_path, role}, ...],
    -- "fields": {material, method, quantity, quantity_bound},
    -- "motivation"/"testable_by" (hypothesis only)}. Local coordinates
    -- (chunk_id) live HERE, never in the published graph.
    grounding        JSONB,
    -- {dep_ref_id: artifact trusty code} at sign time — a dependency's code
    -- changing IS the dirty signal for the topo re-mint cascade.
    dependency_codes JSONB,
    trusty_uri       TEXT,
    artifact_id      BIGINT,
    batch_id         BIGINT,
    state            TEXT        NOT NULL DEFAULT 'candidate'
        CHECK (state IN ('candidate', 'reviewed', 'signed', 'anchored',
                         'published', 'superseded', 'retracted', 'rejected')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one non-terminal publish row per hub.
CREATE UNIQUE INDEX IF NOT EXISTS nanopub_publish_one_live_per_hub
    ON nanopub_publish (claim_ref_id)
    WHERE state NOT IN ('superseded', 'retracted', 'rejected');

CREATE INDEX IF NOT EXISTS nanopub_publish_state_idx
    ON nanopub_publish (state);

COMMENT ON TABLE nanopub_publish IS
    'One live publish row per claim hub: frozen approved string, claim_sha '
    'drift gate, AIDA URI, grounding, and the mint/publish state machine. '
    'Working copy vs frozen artifact bytes is the duplication the crypto '
    'requires (docs/backlog/claim-publication-nanopub-ots.md).';

CREATE TABLE IF NOT EXISTS nanopub_artifacts (
    id              BIGSERIAL   PRIMARY KEY,
    publish_id      BIGINT      NOT NULL REFERENCES nanopub_publish(id),
    claim_ref_id    BIGINT      NOT NULL,
    artifact_type   TEXT        NOT NULL,
    -- The exact serialized signed TriG bytes. The authority; never a
    -- re-serialization.
    trig_bytes      BYTEA       NOT NULL,
    byte_sha256     TEXT        GENERATED ALWAYS AS
                        (encode(sha256(trig_bytes), 'hex')) STORED,
    -- Indexed extracts (audited against the bytes; bytes win on mismatch).
    trusty_uri      TEXT        NOT NULL UNIQUE,
    aida_uri        TEXT        NOT NULL,
    claim_sha       TEXT        NOT NULL,
    signer          TEXT        NOT NULL,
    key_fingerprint TEXT        NOT NULL,
    dois            JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS nanopub_artifacts_publish_idx
    ON nanopub_artifacts (publish_id);
CREATE INDEX IF NOT EXISTS nanopub_artifacts_aida_idx
    ON nanopub_artifacts (aida_uri);

DROP TRIGGER IF EXISTS nanopub_artifacts_append_only ON nanopub_artifacts;
CREATE TRIGGER nanopub_artifacts_append_only
    BEFORE UPDATE OR DELETE ON nanopub_artifacts
    FOR EACH ROW EXECUTE FUNCTION nanopub_append_only();

COMMENT ON TABLE nanopub_artifacts IS
    'Append-only signed-artifact store: exact TriG bytes + indexed '
    'extracts. byte_sha256 is generated from the bytes and doubles as the '
    'OTS leaf digest. Superseded artifacts stay forever.';

CREATE TABLE IF NOT EXISTS nanopub_ots_batches (
    id           BIGSERIAL   PRIMARY KEY,
    merkle_root  TEXT        NOT NULL,
    -- The construction rule: hash fn, tree builder + library version,
    -- ordering, odd-node handling. A bare hash list cannot reproduce a
    -- root; this column is part of the proof.
    construction TEXT        NOT NULL,
    leaf_count   INT         NOT NULL CHECK (leaf_count > 0),
    calendar_url TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS nanopub_ots_batches_append_only ON nanopub_ots_batches;
CREATE TRIGGER nanopub_ots_batches_append_only
    BEFORE UPDATE OR DELETE ON nanopub_ots_batches
    FOR EACH ROW EXECUTE FUNCTION nanopub_append_only();

CREATE TABLE IF NOT EXISTS nanopub_ots_leaves (
    id          BIGSERIAL   PRIMARY KEY,
    batch_id    BIGINT      NOT NULL REFERENCES nanopub_ots_batches(id),
    artifact_id BIGINT      NOT NULL REFERENCES nanopub_artifacts(id),
    leaf_index  INT         NOT NULL,
    -- sha256 hex of the artifact's exact bytes (== artifacts.byte_sha256;
    -- the recompute audit checks the tie).
    leaf_hash   TEXT        NOT NULL,
    -- Serialized opentimestamps Timestamp for this leaf: the sibling path
    -- from the leaf digest up to the batch root. An item's proof IS its
    -- inclusion path — irreplaceable, exists nowhere else.
    path_proof  BYTEA       NOT NULL,
    UNIQUE (batch_id, leaf_index)
);

CREATE INDEX IF NOT EXISTS nanopub_ots_leaves_artifact_idx
    ON nanopub_ots_leaves (artifact_id);

DROP TRIGGER IF EXISTS nanopub_ots_leaves_append_only ON nanopub_ots_leaves;
CREATE TRIGGER nanopub_ots_leaves_append_only
    BEFORE UPDATE OR DELETE ON nanopub_ots_leaves
    FOR EACH ROW EXECUTE FUNCTION nanopub_append_only();

CREATE TABLE IF NOT EXISTS nanopub_ots_proofs (
    id         BIGSERIAL   PRIMARY KEY,
    batch_id   BIGINT      NOT NULL REFERENCES nanopub_ots_batches(id),
    state      TEXT        NOT NULL CHECK (state IN ('pending', 'upgraded')),
    -- Serialized root Timestamp (calendar attestation included): the
    -- `.ots` binary. An upgrade INSERTs a new 'upgraded' row; the
    -- 'pending' row is history, never rewritten.
    ots_proof  BYTEA       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS nanopub_ots_proofs_batch_idx
    ON nanopub_ots_proofs (batch_id, state);

DROP TRIGGER IF EXISTS nanopub_ots_proofs_append_only ON nanopub_ots_proofs;
CREATE TRIGGER nanopub_ots_proofs_append_only
    BEFORE UPDATE OR DELETE ON nanopub_ots_proofs
    FOR EACH ROW EXECUTE FUNCTION nanopub_append_only();

ALTER TABLE nanopub_publish
    DROP CONSTRAINT IF EXISTS nanopub_publish_artifact_id_fkey;
ALTER TABLE nanopub_publish
    ADD CONSTRAINT nanopub_publish_artifact_id_fkey
    FOREIGN KEY (artifact_id) REFERENCES nanopub_artifacts(id);
ALTER TABLE nanopub_publish
    DROP CONSTRAINT IF EXISTS nanopub_publish_batch_id_fkey;
ALTER TABLE nanopub_publish
    ADD CONSTRAINT nanopub_publish_batch_id_fkey
    FOREIGN KEY (batch_id) REFERENCES nanopub_ots_batches(id);

COMMIT;

-- End of 0128_nanopub_publish.sql
