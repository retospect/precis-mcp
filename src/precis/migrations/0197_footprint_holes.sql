-- 0197_footprint_holes.sql
--
-- Footprint-owned non-plated holes (standoff / peg holes). They were
-- dropped from the stored footprint and baked into absolute board-level
-- pcb_features rows at the part's import pose, so moving the part left
-- the hole behind. Holes now live on the footprint, footprint-local mm
-- ``[{x, y, dia_mm}]``, and the IR maps them to world per instance
-- (precis.pcb.pose). Both footprint sources carry them: the design-local
-- table and the global catalog cache.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE pcb_local_footprints
    ADD COLUMN IF NOT EXISTS holes jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE part_footprints
    ADD COLUMN IF NOT EXISTS holes jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN pcb_local_footprints.holes IS
    'Footprint-local non-plated holes [{x, y, dia_mm}] (mm); mapped to '
    'world per instance through precis.pcb.pose.';
COMMENT ON COLUMN part_footprints.holes IS
    'Footprint-local non-plated holes [{x, y, dia_mm}] (mm); mapped to '
    'world per instance through precis.pcb.pose.';

COMMIT;

-- End of 0197_footprint_holes.sql
