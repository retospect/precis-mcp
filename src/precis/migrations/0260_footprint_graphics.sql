-- 0260_footprint_graphics.sql
--
-- Footprint-owned graphics (silk/courtyard/fab/doc/document primitives,
-- text, keepout/rule regions). The EasyEDA readers dropped every record
-- that was not a pad/hole — a footprint's "no copper" zone or keepout
-- label was uninspectable (gr520150). Graphics live on the footprint,
-- footprint-local mm ``[{kind, layer, ...}]`` (Y up, as the pads), and
-- map to world per instance through precis.pcb.pose — inspectable only:
-- they bind nothing yet (keepout-binds-in-placement is a later slice).
-- Both footprint sources carry them: the design-local table and the
-- global catalog cache.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE pcb_local_footprints
    ADD COLUMN IF NOT EXISTS graphics jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE part_footprints
    ADD COLUMN IF NOT EXISTS graphics jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN pcb_local_footprints.graphics IS
    'Footprint-local graphics [{kind, layer, ...}] (mm, Y up): silk/'
    'courtyard/fab/doc lines, arcs, polygons, regions, text and keepout/'
    'rule regions. Inspectable only — binds nothing.';
COMMENT ON COLUMN part_footprints.graphics IS
    'Footprint-local graphics [{kind, layer, ...}] (mm, Y up): silk/'
    'courtyard/fab/doc lines, arcs, polygons, regions, text and keepout/'
    'rule regions. Inspectable only — binds nothing.';

COMMIT;

-- End of 0260_footprint_graphics.sql
