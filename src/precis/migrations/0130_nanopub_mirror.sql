-- 0130_nanopub_mirror.sql
--
-- Registry mirror — a read-only cache of OTHER PEOPLE's published
-- nanopubs (docs/backlog/nanopub-registry-mirror.md). External nanopubs
-- are frozen by construction (the artifact code IS the content hash),
-- so unlike `nanopub_artifacts` there is NO append-only trigger: this
-- is a cache, not our proof store — a re-fetch may overwrite a row that
-- failed verification. Everything except (code, bytes, source, time) is
-- a rebuildable index extract.
--
--   * `nanopub_mirror` — one row per fetched artifact code. `verified`
--     = the trusty hash recomputed over the fetched bytes matches the
--     code (a mismatch is a corrupt/hostile mirror response: kept,
--     flagged, never indexed as valid). `retracted_by`/`superseded_by`
--     are DERIVED flags, set only when a retracting/superseding edge's
--     signer matches the target's signer (the authoritative-retraction
--     rule) — flags, not exclusions.
--
--   * `nanopub_mirror_edges` — np→np references extracted from the
--     bytes (retracts / supersedes / refers-to). `to_code` is
--     deliberately NOT an FK: arrival order is open-world (a retraction
--     can be fetched before its target), and anyone may publish an
--     `npx:retracts` at someone else's nanopub.
--
-- Forward-only (ADR 0005): additive, no data migration.

BEGIN;

CREATE TABLE IF NOT EXISTS nanopub_mirror (
    artifact_code        TEXT        PRIMARY KEY,
    trig_bytes           BYTEA       NOT NULL,
    byte_sha256          TEXT        GENERATED ALWAYS AS
                             (encode(sha256(trig_bytes), 'hex')) STORED,
    source_url           TEXT        NOT NULL,
    fetched_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified             BOOLEAN     NOT NULL DEFAULT FALSE,
    -- Rebuildable extracts (parse-leniently: NULL when unparseable).
    aida_uri             TEXT,
    signer               TEXT,
    key_fingerprint      TEXT,
    dois                 JSONB,
    assertion_predicates JSONB,
    -- Derived (authoritative-retraction rule; see migration comment).
    retracted_by         TEXT,
    superseded_by        TEXT
);

CREATE INDEX IF NOT EXISTS nanopub_mirror_aida_idx
    ON nanopub_mirror (aida_uri) WHERE aida_uri IS NOT NULL;
CREATE INDEX IF NOT EXISTS nanopub_mirror_signer_idx
    ON nanopub_mirror (signer) WHERE signer IS NOT NULL;

COMMENT ON TABLE nanopub_mirror IS
    'Read-only cache of external published nanopubs: exact fetched bytes '
    '+ trusty-recompute verification + rebuildable index extracts '
    '(docs/backlog/nanopub-registry-mirror.md). Not our proof store; '
    'no append-only trigger.';

CREATE TABLE IF NOT EXISTS nanopub_mirror_edges (
    id        BIGSERIAL PRIMARY KEY,
    from_code TEXT      NOT NULL
        REFERENCES nanopub_mirror(artifact_code) ON DELETE CASCADE,
    to_code   TEXT      NOT NULL,
    relation  TEXT      NOT NULL
        CHECK (relation IN ('retracts', 'supersedes', 'refers-to')),
    UNIQUE (from_code, to_code, relation)
);

CREATE INDEX IF NOT EXISTS nanopub_mirror_edges_to_idx
    ON nanopub_mirror_edges (to_code);

COMMENT ON TABLE nanopub_mirror_edges IS
    'np→np references extracted from mirrored bytes. to_code is not an '
    'FK (open-world arrival order; multiple retraction claimants).';

COMMIT;

-- End of 0130_nanopub_mirror.sql
