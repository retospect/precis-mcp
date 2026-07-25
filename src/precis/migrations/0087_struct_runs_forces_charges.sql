-- 0087_struct_runs_forces_charges.sql
--
-- gripe 161576 — per-atom FORCES on a compute run, for a *qualitative* sense
-- of which atoms are under strain / doing the work (the modeling LLM's read),
-- not physics-grade truth — a real MLIP/DFT relax runs later. Mirrors the
-- ``final_geometry`` (0044) precedent: a canonical-rank-indexed jsonb array.
--
--   * forces  — {"vectors": [[fx,fy,fz], ...], "approx": bool, "source": str}
--     eV/Å cartesian, indexed by the same canonical rank as ``final_geometry``.
--     ``approx: true`` marks a cheap single-point ASE-EMT estimate (the
--     ``clean`` rung has no calculator of its own); ``approx: false`` is a
--     real emt/ml relax's own force. NULL when neither is available (no run
--     yet, or the element set falls outside EMT's coverage).
--   * charges — reserved for a future charge-bearing rung (DFT+Bader). No
--     backend produces partial charges today, so this column stays NULL —
--     never fabricated.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE struct_runs
    ADD COLUMN IF NOT EXISTS forces  jsonb,
    ADD COLUMN IF NOT EXISTS charges jsonb;

COMMENT ON COLUMN struct_runs.forces IS
    'Per-atom force vectors (eV/Å, cartesian), canonical-rank-indexed like '
    'final_geometry (0044): {"vectors": [[fx,fy,fz], ...], "approx": bool, '
    '"source": str}. approx=true is a cheap EMT single-point estimate (the '
    'clean rung has no calculator); approx=false is a real emt/ml relax '
    'force. NULL when neither is available.';
COMMENT ON COLUMN struct_runs.charges IS
    'Reserved for a future charge-bearing rung (DFT+Bader, etc.) — no '
    'backend produces partial charges today, so this is always NULL. Never '
    'fabricate a value here.';

COMMIT;

-- End of 0087_struct_runs_forces_charges.sql
