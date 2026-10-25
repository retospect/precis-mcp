-- 0250_struct_neb.sql
--
-- NEB structure builder, slice 3 (gr512126,
-- docs/backlog/neb-structure-builder.md): the compute record for one CI-NEB
-- edge between two agent-authored `structure` endpoints. The spec numbers
-- this 0248; 0248/0249 were reserved by sibling branches, so it lands here.
--
--   * struct_neb_runs   — one row per NEB edge run: the IS/FS/TS design
--     triple, the endpoint relax runs the band started from, the params,
--     the priced scalars (barriers NULL when the band was never priced —
--     a collapsed band has no transition state), the per-check trust
--     records (autocatpath.trust record shape, one per check), and the
--     content address that makes a repeat `mode='neb'` a cache hit.
--   * struct_neb_images — the relaxed band: one row per image (0 = IS …
--     n+1 = FS) with energy, max-force and the canonical-rank-indexed
--     positions (same convention as struct_runs.final_geometry, 0044).
--
-- NEB rows deliberately stay OUT of `struct_runs` (spec §Data model): every
-- relax reader there treats `energy` as a relaxed minimum —
-- `harvest_measures` and the cache-first relax lookup included — so an
-- E_TS in that column would read as a relax result.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS struct_neb_runs (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    is_ref_id       bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    fs_ref_id       bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    ts_ref_id       bigint REFERENCES refs (ref_id) ON DELETE CASCADE,
    is_run_id       bigint REFERENCES struct_runs (id) ON DELETE SET NULL,
    fs_run_id       bigint REFERENCES struct_runs (id) ON DELETE SET NULL,
    is_version      integer,                 -- IS design version minted
                                             -- against (the drift contract)
    fs_version      integer,                 -- FS design version minted
                                             -- against
    fidelity        text NOT NULL,           -- 'ml' only (spec §Compute)
    model           text,
    params          jsonb NOT NULL DEFAULT '{}'::jsonb,
    status          text NOT NULL DEFAULT 'queued',  -- queued|running|succeeded|failed
    converged       boolean NOT NULL DEFAULT FALSE,
    barrier_fwd_ev  double precision,        -- E(TS) - E(IS); NULL = not priced
    barrier_rev_ev  double precision,        -- E(TS) - E(FS); NULL = not priced
    delta_e_ev      double precision,        -- E(FS) - E(IS); NULL = not priced
    ts_index        integer,                 -- band index of the TS image
    ts_fmax         double precision,        -- true residual on the TS image
    n_imag          integer,                 -- slice 5: imaginary-mode count
    imag_cm1        double precision,        -- slice 5: the imaginary frequency
    trust           jsonb NOT NULL DEFAULT '[]'::jsonb,  -- one record per check
    trusted         boolean NOT NULL DEFAULT FALSE,
    multistart      jsonb,                   -- k-start aggregation evidence
    cache_key       text,                    -- sha256(IS sha, FS sha, fidelity,
                                             -- model, params, code version,
                                             -- engine token)
    engine          text,                    -- the precis-side autocatpath
                                             -- engine token folded into
                                             -- cache_key at mint; != the
                                             -- current token renders 'stale,
                                             -- rerun on current'
    wall_s          double precision,
    job_ref_id      bigint REFERENCES refs (ref_id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE struct_neb_runs IS
    'gr512126 slice 3: one CI-NEB edge run between two agent-authored '
    'structure endpoints (IS/FS/TS design triple). Priced scalars NULL when '
    'the band was never priced (collapsed/not converged); trust holds one '
    'autocatpath.trust-shaped record per check. Kept out of struct_runs so '
    'an E_TS never reads as a relaxed minimum.';
COMMENT ON COLUMN struct_neb_runs.cache_key IS
    'Content address: sha256(is structure_sha, fs structure_sha, fidelity, '
    'model, params, code version, engine token). An identical mode=''neb'' '
    'request is a zero-compute cache hit; an engine bump re-keys the '
    'address, so an old-engine run is never a hit.';
COMMENT ON COLUMN struct_neb_runs.is_version IS
    'The IS design version the edge was minted against; the band view '
    'loads the endpoints at these versions, not the live ones.';
COMMENT ON COLUMN struct_neb_runs.engine IS
    'The precis-side engine token (precis.quest.compute.'
    '_autocatpath_engine_token: the PRECIS_AUTOCATPATH_VERSION env or the '
    'autocatpath pin floor) folded into cache_key at mint. A run whose '
    'token differs from the mint host''s current one renders ''stale, '
    'rerun on current'' (strict-engine ruling, gr517804).';
COMMENT ON COLUMN struct_neb_runs.trust IS
    'One record per run-trust check, autocatpath.trust record shape '
    '(check/verdict/severity/seed/attempt/step/evidence/id, plus an ''engine'' '
    'stamp for the engine-version staleness rule — a record minted under a '
    'different autocatpath version renders ''stale, rerun on current'').';
COMMENT ON COLUMN struct_neb_runs.n_imag IS
    'Slice 5 (saddle frequency check) — NULL on runs predating it; the read '
    'renders ''frequency: not checked''.';
COMMENT ON COLUMN struct_neb_runs.job_ref_id IS
    'The minted struct_neb job (kind=''job'' ref) that ran this edge; NULL '
    'for a row written before the job id was known.';

-- One in-flight run per cache_key is enforced at mint (a queued/running
-- lookup + the job's idem_key); the table only hard-guards the *priced*
-- address — a second succeeded row for the same key is a duplicate answer.
CREATE UNIQUE INDEX IF NOT EXISTS struct_neb_runs_key_succeeded
    ON struct_neb_runs (cache_key) WHERE status = 'succeeded';
CREATE INDEX IF NOT EXISTS struct_neb_runs_is_idx
    ON struct_neb_runs (is_ref_id, id DESC);
CREATE INDEX IF NOT EXISTS struct_neb_runs_fs_idx
    ON struct_neb_runs (fs_ref_id, id DESC);
CREATE INDEX IF NOT EXISTS struct_neb_runs_ts_idx
    ON struct_neb_runs (ts_ref_id) WHERE ts_ref_id IS NOT NULL;
-- The quest daily cap (PRECIS_STRUCT_NEB_QUEST_DAILY) counts mints by the
-- stamped quest; keyed here so the rolling-24h count stays an index probe.
CREATE INDEX IF NOT EXISTS struct_neb_runs_quest_idx
    ON struct_neb_runs ((params->>'quest_ref_id'), created_at DESC)
    WHERE params->>'quest_ref_id' IS NOT NULL;

CREATE TABLE IF NOT EXISTS struct_neb_images (
    run_id      bigint NOT NULL REFERENCES struct_neb_runs (id) ON DELETE CASCADE,
    idx         integer NOT NULL,            -- 0 = IS … n+1 = FS
    energy_ev   double precision,            -- image energy; NULL when unpriced
    max_force   double precision,            -- eV/Å on the image
    positions   jsonb,                       -- canonical-rank frac rows +
                                            -- lattice (struct_runs.
                                            -- final_geometry shape)
    PRIMARY KEY (run_id, idx)
);
COMMENT ON TABLE struct_neb_images IS
    'gr512126 slice 3: the relaxed NEB band — one row per image, idx 0 the '
    'IS and n+1 the FS. positions is canonical-rank-indexed like '
    'struct_runs.final_geometry (0044): {"frac": [[...]], "lattice": [[...]]}.';

COMMIT;

-- End of 0250_struct_neb.sql
