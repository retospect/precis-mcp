-- 0290_struct_md.sql
--
-- Molecular-dynamics job (gr606465): the compute record for one NVT
-- Langevin trajectory on a held or free structure. The structure surface
-- had relax (EMT/xTB/MACE/CHGNet) and CI-NEB but no MD rung, so ML-MD
-- for membranes and electrode interfaces could not run (the 2026-11-09
-- ALCHEM Lab gap analysis; docs/backlog/electrochem-mlip-ladder.md).
--
--   * struct_md_runs — one row per MD run: the design + version it ran
--     on, the rung (fidelity/model), the engine params, the measured
--     scalars (thermostat set point vs measured mean temperature,
--     mean/std potential energy), the MSD and RDF summaries, the extXYZ
--     trajectory itself, and the content address that makes a repeat
--     `mode='md'` a zero-compute cache hit.
--
-- MD rows deliberately stay OUT of `struct_runs` (same rule as
-- struct_neb_runs, 0250): every relax reader there treats `energy` as a
-- relaxed minimum — a mean potential energy would read as one.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS struct_md_runs (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    structure_ref_id bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    on_version       integer,                 -- design version minted against
                                             -- (the drift contract)
    fidelity         text NOT NULL,           -- 'ml' | 'emt'
    model            text,
    params           jsonb NOT NULL DEFAULT '{}'::jsonb,
    status           text NOT NULL DEFAULT 'queued',  -- queued|running|succeeded|failed
    n_steps          integer,                 -- integration steps completed
    n_frames         integer,                 -- trajectory frames stored
    temperature_k    double precision,        -- thermostat set point
    temp_mean_k      double precision,        -- measured mean over samples
    e_pot_mean_ev    double precision,        -- mean potential energy
    e_pot_std_ev     double precision,        -- its sample std-dev
    msd              jsonb,                   -- {"t_ps","msd_A2","n_mobile",
                                             --  "final_A2"}
    rdf              jsonb,                   -- {"rmax","nbins","pairs":
                                             --  {pair:{r,g,n_pairs}}}
    trajectory       text,                    -- the extXYZ frames
    cache_key        text,                    -- sha256(structure sha, fidelity,
                                             -- model, params, code version)
    wall_s           double precision,
    job_ref_id       bigint REFERENCES refs (ref_id) ON DELETE SET NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE struct_md_runs IS
    'gr606465: one NVT Langevin MD run on a structure design. MSD/RDF '
    'summaries and the extXYZ trajectory live on the row; NULL scalars '
    'mean the run never produced a trajectory. Kept out of struct_runs '
    'so a mean potential energy never reads as a relaxed minimum.';
COMMENT ON COLUMN struct_md_runs.cache_key IS
    'Content address: sha256(structure sha, fidelity, model, params, '
    'code version). An identical mode=''md'' request is a zero-compute '
    'cache hit.';
COMMENT ON COLUMN struct_md_runs.params IS
    'The normalized engine knob set (md.normalize_md_params: '
    'temperature_k/timestep_fs/steps/friction_per_fs/traj_every/'
    'rdf_rmax_A/rdf_nbins/seed) plus request-context stamps — never '
    'part of the cache key.';
COMMENT ON COLUMN struct_md_runs.trajectory IS
    'The sampled frames as extXYZ (frame every params.traj_every '
    'steps); NULL until the run succeeds — the trajectory handle '
    'view=''md'', args={''run'': N, ''trajectory'': true} serves.';
COMMENT ON COLUMN struct_md_runs.job_ref_id IS
    'The minted struct_md job (kind=''job'' ref) that ran this '
    'trajectory; NULL for a row written before the job id was known.';

-- One in-flight run per cache_key is enforced at mint (a queued/running
-- lookup + the job's idem_key); the table only hard-guards the *priced*
-- address — a second succeeded row for the same key is a duplicate
-- answer (same contract as struct_neb_runs_key_succeeded, 0250).
CREATE UNIQUE INDEX IF NOT EXISTS struct_md_runs_key_succeeded
    ON struct_md_runs (cache_key) WHERE status = 'succeeded';
CREATE INDEX IF NOT EXISTS struct_md_runs_ref_idx
    ON struct_md_runs (structure_ref_id, id DESC);

-- The job_ref_id FK wants its own covering index too (a parent DELETE on
-- refs would seq-scan the child to null it — the ratchet 0251 fixed for
-- struct_neb_runs; the structure_ref_id FK is already covered by the
-- leading column of struct_md_runs_ref_idx). Partial on NOT NULL: the
-- column is NULL until the job id is known.
CREATE INDEX IF NOT EXISTS struct_md_runs_job_idx
    ON struct_md_runs (job_ref_id) WHERE job_ref_id IS NOT NULL;

COMMIT;

-- End of 0290_struct_md.sql
