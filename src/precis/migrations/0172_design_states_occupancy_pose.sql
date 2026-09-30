-- 0172_design_states_occupancy_pose.sql
--
-- Two per-state slots on `design_states` for the se walker
-- (docs/backlog/se-walker-light-protocol.md):
--
--   occupancy  jsonb  — {"<strand>.<ord>": "<helix>@<offset>" | null}: which
--                       foothold each leg domain sits on in this state (null =
--                       a free leg). Authored, through `declare_states` /
--                       `declare_stations`; the domain's read time override.
--   pose       jsonb  — {"xyz": [m, m, m], "rot": [rad, rad, rad]}: the owning
--                       block's own (parent-relative) pose in this state.
--                       DERIVED, never authored: written only by
--                       `relax_chain(state=)` through `set_state_pose`, and
--                       preserved by the `declare_states` upsert (a
--                       re-declaration never wipes a relaxed pose).
--
-- Additive, forward-only (ADR 0005), idempotent. Regenerate the baseline
-- snapshot after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE design_states ADD COLUMN IF NOT EXISTS occupancy jsonb;
ALTER TABLE design_states ADD COLUMN IF NOT EXISTS pose jsonb;

COMMIT;
