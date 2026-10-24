-- 0240_openalex_edges.sql
--
-- OpenAlex snapshot cites loader, slice 1
-- (docs/backlog/openalex-snapshot-cites.md): staging for citation edges
-- streamed out of the public ``s3://openalex`` works dump, plus a per-file
-- checkpoint so the 1,898-part pass is resumable and idempotent.
--
-- ``openalex_cite_edges`` holds one row per (citing -> cited) work pair where
-- AT LEAST ONE endpoint matched a held paper (``ref_identifiers``
-- ``id_kind IN ('openalex','doi')``). OpenAlex ids are stored short-form
-- (``W123``, as ``ref_identifiers`` keeps them). ``src_doi`` is the citing
-- work's DOI lowercased — kept so gr513755's stub-trickle and gr513769's
-- cited-by seeds can rank unheld citers without a second resolution pass.
-- ``minted_at`` is stamped when slice 2 turns a both-held row into a
-- ``cites`` link through the shared link writer.
--
-- ``openalex_snapshot_files`` checkpoints one ``part_NNNN.gz`` per
-- ``(snapshot_date, s3_key)``: a file is re-read only when no ``done`` row
-- with the same ``content_length`` exists, so a re-release moving bytes
-- into a file reprocesses it while an identical file is skipped. ``status``
-- is ``done`` or ``failed``; a ``failed`` row never skips.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS openalex_cite_edges (
    src_oa_id     text NOT NULL,
    dst_oa_id     text NOT NULL,
    src_ref_id    bigint REFERENCES refs(ref_id),
    dst_ref_id    bigint REFERENCES refs(ref_id),
    src_doi       text,
    snapshot_date date NOT NULL,
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    minted_at     timestamptz,
    PRIMARY KEY (src_oa_id, dst_oa_id),
    CHECK (src_ref_id IS NOT NULL OR dst_ref_id IS NOT NULL)
);

COMMENT ON TABLE openalex_cite_edges IS
    'Citation edges streamed from the OpenAlex works snapshot (slice 1 of '
    'openalex-snapshot-cites): kept when either endpoint resolves to a held '
    'paper via ref_identifiers openalex/doi. src_oa_id/dst_oa_id short-form '
    'W-ids; src_doi lowercased for stub seeding; minted_at stamps the '
    'slice-2 cites-link write.';

-- dst_oa_id lookups (in-corpus cite count of a stub, gr513755) can't ride
-- the (src, dst) PK.
CREATE INDEX IF NOT EXISTS openalex_cite_edges_dst_oa
    ON openalex_cite_edges (dst_oa_id);
CREATE INDEX IF NOT EXISTS openalex_cite_edges_src_ref
    ON openalex_cite_edges (src_ref_id) WHERE src_ref_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS openalex_cite_edges_dst_ref
    ON openalex_cite_edges (dst_ref_id) WHERE dst_ref_id IS NOT NULL;
-- The slice-2 minting scan: both endpoints held, not yet linked.
CREATE INDEX IF NOT EXISTS openalex_cite_edges_unminted
    ON openalex_cite_edges (src_ref_id, dst_ref_id)
    WHERE minted_at IS NULL
      AND src_ref_id IS NOT NULL
      AND dst_ref_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS openalex_snapshot_files (
    snapshot_date  date NOT NULL,
    s3_key         text NOT NULL,
    content_length bigint NOT NULL,
    records_seen   bigint,
    edges_kept     bigint,
    status         text NOT NULL CHECK (status IN ('done', 'failed')),
    error          text,
    done_at        timestamptz,
    PRIMARY KEY (snapshot_date, s3_key)
);

COMMENT ON TABLE openalex_snapshot_files IS
    'Per-part-file checkpoint for the OpenAlex snapshot loader: a done row '
    'with matching content_length skips the file on re-run; failed rows '
    'record the error and never skip.';

COMMIT;

-- End of 0240_openalex_edges.sql
