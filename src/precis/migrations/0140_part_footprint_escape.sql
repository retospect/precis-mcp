-- 0140_part_footprint_escape.sql
--
-- docs/backlog/pcb-guided-place-route.md Slice 5 — cache the precomputed
-- footprint escape graph (shell decomposition, per-gap capacity,
-- required_layers) alongside the parsed footprint. Escape routing is
-- footprint-intrinsic (decisions log, 2026-08-27): it follows from the
-- pads alone, so it is computed once per footprint by
-- `precis.pcb.escape.compute_escape_graph` and cached here rather than
-- recomputed every time a placement or L1/L4 estimator needs it.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE part_footprints ADD COLUMN IF NOT EXISTS escape jsonb;

COMMENT ON COLUMN part_footprints.escape IS
    'Precomputed footprint escape graph (precis.pcb.escape.EscapeGraph, '
    'shells/gaps/per_shell_capacity/required_layers) — footprint-intrinsic, '
    'cached once per footprint, never recomputed per placement.';

COMMIT;

-- End of 0140_part_footprint_escape.sql
