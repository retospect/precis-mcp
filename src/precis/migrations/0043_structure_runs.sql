-- 0043_structure_runs.sql
--
-- ADR 0043 §9/§12 — the COMPUTE system-of-record for the `structure` kind.
-- A `relax`/NEB/MD pass is a *run*: it consumes a design at one version and
-- emits derived results (energy, forces, the relaxed geometry). The design
-- itself stays the editable intent (struct_atoms/struct_bonds, 0042); the run
-- is the immutable audit of one fidelity rung applied to it.
--
--   * struct_runs   — one row per compute pass: fidelity rung, status, the
--     scalar outputs (energy/max_force/converged/steps), the backend model,
--     and the design version it ran against. Energy/forces are NULLable —
--     the rung-0 `clean` geometry repair has *no* energy ("undefined until
--     it is", ADR §6 q9), and a failed run has none either.
--   * struct_frames — the per-step CONVERGENCE CURVE (energy + max_force per
--     optimiser step). `positions` is NULL for a plain relax (we keep the
--     curve + the final geometry on the design, not every intermediate
--     geometry, ADR §6.9) and carries geometry only for MD/NEB trajectories.
--
-- The relaxed geometry is written back onto the design's struct_atoms (the
-- run mutates intent toward the relaxed state); struct_runs preserves the
-- envelope so a later diff(before, after) and the fidelity-ladder history
-- stay legible.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

-- 1. runs — one per compute pass over a design -----------------------------
CREATE TABLE IF NOT EXISTS struct_runs (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id      bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    fidelity    text NOT NULL,             -- clean|ff|xtb|ml|dft-fast|dft-tight
    status      text NOT NULL DEFAULT 'succeeded',  -- succeeded|failed|running
    model       text,                      -- MLIP/functional (NULL for clean)
    on_version  integer NOT NULL,          -- the design version this ran against
    converged   boolean NOT NULL DEFAULT FALSE,
    n_steps     integer NOT NULL DEFAULT 0,
    energy      double precision,          -- eV; NULL = undefined (clean/failed)
    max_force   double precision,          -- eV/Å; NULL for the geometry rung
    max_disp    double precision,          -- Å, largest atomic move
    params      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE struct_runs IS
    'ADR 0043 §9/§12: one compute pass (relax/NEB/MD) over a structure design '
    'at a fixed version. Derived scalars live here, never on the mutable atom '
    'row. Energy/forces NULLable — the clean geometry rung has none.';
CREATE INDEX IF NOT EXISTS struct_runs_ref_idx
    ON struct_runs (ref_id, id DESC);

-- 2. frames — the per-step convergence curve / trajectory ------------------
CREATE TABLE IF NOT EXISTS struct_frames (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      bigint NOT NULL REFERENCES struct_runs (id) ON DELETE CASCADE,
    step        integer NOT NULL,
    energy      double precision,          -- eV per step (NULL for clean)
    max_force   double precision,          -- eV/Å per step (rung-0: max move proxy)
    positions   jsonb,                     -- NULL for relax; geometry for MD/NEB
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS struct_frames_run_idx
    ON struct_frames (run_id, step);

COMMIT;

-- End of 0043_structure_runs.sql
