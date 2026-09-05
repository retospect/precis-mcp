-- 0152_component_geometry_specs.sql
--
-- Universal geometric-extent specs for `component`
-- (docs/backlog/se-off-the-shelf-fabrication.md, engine 1 rung 2). 0093
-- seeded the *selection* specs an agent shops by (thread_size, grade,
-- max_working_pressure); none of them says how big the thing is, so
-- nothing downstream can turn a catalog row into a solid. These ten are
-- the dimensions a bought part's envelope generator reads.
--
-- `category_id IS NULL` (universal) is deliberate and forced: a spec_id
-- is a primary key with ONE category, and an outside diameter means the
-- same thing on a screw shank, a pipe, a washer and a bearing race. They
-- are extents, like `mass` and `length_overall`.
--
-- Unit wart, recorded rather than papered over: these are **mm**, which
-- matches every other length spec in 0093's per-category seed
-- (thread_pitch, length, bore_diameter, min_bend_radius) but NOT the
-- universal `length_overall`, which is metres. 0093 is sealed
-- (forward-only, ADR 0005) and consistency with the neighbours a
-- generator reads alongside these beats consistency with the one
-- outlier. Consumers must read `component_specs.canonical_unit` and
-- convert rather than assume — the reserved unit-conversion layer
-- (`component_spec_values.input_unit`) is the eventual fix, not this
-- migration.
--
-- Overlaps with 0093, named so they are not mistaken for duplicates:
-- `bore_diameter` (hose) and `bore_diameter_bearing` (bearing) are
-- category-scoped *functional* bores that carry their own selection
-- meaning; `inner_diameter` is the geometric hole a generator subtracts.
-- A row may legitimately carry both.
--
-- Forward-only (ADR 0005). Idempotent (ON CONFLICT DO NOTHING).

BEGIN;

INSERT INTO component_specs
    (spec_id, name, canonical_unit, dimension, value_type, status, description)
VALUES
    ('outer_diameter', 'Outer diameter', 'mm', 'length', 'quantity', 'core',
     'Nominal outside diameter of the part''s round envelope — screw '
     'shank, pipe/tube OD, washer OD, bearing outer race.'),
    ('inner_diameter', 'Inner diameter', 'mm', 'length', 'quantity', 'core',
     'Nominal through-hole diameter — washer ID, pipe bore, bearing bore. '
     'The geometric hole, distinct from the category-scoped functional '
     'bores (bore_diameter, bore_diameter_bearing).'),
    ('wall_thickness', 'Wall thickness', 'mm', 'length', 'quantity', 'core',
     'Wall thickness of a hollow section (tube, pipe, extruded profile).'),
    ('thickness', 'Thickness', 'mm', 'length', 'quantity', 'core',
     'Thickness of a flat part — sheet, plate, washer, shim. For stock '
     'sheet this is the discrete series value a cut part is realized at.'),
    ('width', 'Width', 'mm', 'length', 'quantity', 'core',
     'Width of the part''s bounding extent across its section '
     '(rectangular profile, bearing width, strap).'),
    ('height', 'Height', 'mm', 'length', 'quantity', 'core',
     'Height of the part''s bounding extent across its section, '
     'perpendicular to width.'),
    ('across_flats', 'Across flats', 'mm', 'length', 'quantity', 'core',
     'Wrench size — distance between opposing flats of an external hex '
     '(bolt head, nut). The spanner/socket the joint needs.'),
    ('head_diameter', 'Head diameter', 'mm', 'length', 'quantity', 'core',
     'Outside diameter of a fastener head (cap-screw head, washer face). '
     'What a counterbore must clear.'),
    ('head_height', 'Head height', 'mm', 'length', 'quantity', 'core',
     'Head height along the fastener axis — the protrusion above the '
     'clamped face, and the counterbore depth that would bury it.'),
    ('drive_size', 'Drive size', 'mm', 'length', 'quantity', 'core',
     'Size of the internal tool interface — hex-key across flats, Torx '
     'nominal. Pairs with drive_type; distinct from across_flats, which '
     'is the external hex.')
ON CONFLICT (spec_id) DO NOTHING;

COMMIT;

-- End of 0152_component_geometry_specs.sql
