-- 0163_component_head_form_specs.sql
--
-- The four fastener specs a stamped feature needs
-- (docs/backlog/se-off-the-shelf-fabrication.md engine 2b, rung 3c).
-- 0152 seeded the *extents* (head_diameter, head_height, drive_size) —
-- how big the head is. None of them says what SHAPE it is, and the shape
-- is what decides whether the near member gets a counterbore, a 90°
-- countersink or nothing at all. `precis_se.fasten` deferred
-- counterbores/countersinks for exactly this reason ("a head-form
-- question, and the head forms are one flat `fastener` category today");
-- these rows are the answer, and they are specs rather than a category
-- split because a category is about what you shop for, not what the
-- geometry engine reads.
--
-- `point_type` is the other half of the same gap: a machine screw ends
-- in a flat, a tapping screw ends in a point, and a printed member
-- cannot be given the right hole until something says which it is.
--
-- `drive_code` is text, not categorical: the Torx ladder (T6…T100) plus
-- hex sizes is an open list that grows with the series file, and an enum
-- the file can outrun would refuse a legitimate part at mint time.
--
-- `category_id IS NULL` (universal), like 0152's extents, and for one
-- reason beyond consistency with the neighbours a generator reads
-- alongside these. **The baseline snapshot carries schema + the
-- migration ledger, not seed rows** (its only INSERTs are inside
-- function bodies), so on any database built from the baseline
-- `component_categories` is empty while the ledger already says 0093
-- ran. A category-scoped insert here therefore violates
-- `component_specs_category_id_fkey` on the from-scratch-vs-baseline
-- convergence replay — verified, not theorised: that is how this
-- migration first failed the gate. Any later migration seeding a
-- category-scoped row has the same problem, and the fix belongs in the
-- baseline/seed path, not in one more workaround
-- (docs/backlog/component-seed-guard-misses-scoped-specs.md).
--
-- The cost, accepted knowingly and already paid by 0152: nothing stops a
-- pipe from carrying a `head_form`. A head form on a pipe is nonsense
-- that no generator reads, which is a smaller problem than a migration
-- that cannot be replayed.
--
-- Forward-only (ADR 0005). Idempotent (ON CONFLICT DO NOTHING).

BEGIN;

INSERT INTO component_specs
    (spec_id, name, canonical_unit, dimension, value_type, allowed_values,
     status, description)
VALUES
    ('head_form', 'Head form', NULL, 'categorical', 'categorical',
     '["cap", "countersunk", "button", "pan", "hex", "flange", "none"]'::jsonb,
     'core',
     'Shape of a fastener head, which decides what the near member gets: '
     'countersunk => a cone, cap/pan/button => a counterbore or plain '
     'head clearance, none => a set screw with no head at all.'),
    ('point_type', 'Point type', NULL, 'categorical', 'categorical',
     '["machine", "tapping-c", "tapping-f", "thread-forming", "insert"]'::jsonb,
     'core',
     'How a fastener''s far end engages: machine = a formed thread meeting '
     'a nut or a tapped hole; tapping-c = ISO 1478 sharp point, cuts/forms '
     'its own thread in a core hole; tapping-f = blunt/flat point; '
     'thread-forming = rolls a thread in thermoplastic without cutting; '
     'insert = not a screw, a threaded insert the screw meets.'),
    ('head_angle', 'Head angle', 'deg', 'angle', 'quantity', NULL,
     'core',
     'Included angle of a countersunk head — 90 for the metric ISO '
     'families, 82 for the imperial ones. The angle the stamped '
     'countersink is cut at; absent on any other head form.'),
    ('drive_code', 'Drive code', NULL, 'text', 'text', NULL,
     'core',
     'The tool interface by its trade designation — T25, T30 for '
     'hexalobular/Torx. Pairs with drive_size (the millimetre extent) '
     'and drive_type (the family); this is the one you ask for in a shop.')
ON CONFLICT (spec_id) DO NOTHING;

COMMIT;

-- End of 0163_component_head_form_specs.sql
