-- precis_se/0021_se_contracts.sql
--
-- se's adoption of the design core's interface vocabulary
-- (docs/backlog/design-state-core.md item 7a; precis_se.contracts):
--
-- * se_connects.contract — the six-component interface contract
--   {"loads": {Fx, Fy, Fz: N, Mx, My, Mz: N·m}, "provenance": {component:
--   entry}}. An ABSENT component is UNKNOWN, never zero; view='validate'
--   names every unknown component (contract_component_unknown). NULL = no
--   contract declared yet.
-- * se_blocks.insertion — the permitted assembly-direction cone
--   {"direction": unit [x, y, z] in the block's own frame,
--   "half_angle_rad": h, 0 < h <= pi/2}. view='validate' intersects every
--   declared cone in the design frame; an empty intersection is the
--   single-direction-assembly error (insertion_cones_disjoint). NULL =
--   unconstrained.
--
-- Additive and nullable; no data migration.

ALTER TABLE se_connects ADD COLUMN IF NOT EXISTS contract jsonb;
ALTER TABLE se_blocks ADD COLUMN IF NOT EXISTS insertion jsonb;

COMMENT ON COLUMN se_connects.contract IS
    'Six-component interface contract {loads: {Fx,Fy,Fz N; Mx,My,Mz N·m}, '
    'provenance: {component: precis.design.provenance entry}}. Absent '
    'component = unknown, never zero. NULL = none declared.';

COMMENT ON COLUMN se_blocks.insertion IS
    'Permitted assembly-direction cone {direction: unit vector in the '
    'block frame, half_angle_rad: (0, pi/2]}. NULL = unconstrained.';
