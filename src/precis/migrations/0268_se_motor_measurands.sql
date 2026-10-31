-- 0268_se_motor_measurands.sql
--
-- Seeds the measurands se-flagellar-motor.md slice 1 needs: a rotary
-- biomolecular motor as an se component (a stator/rotor revolute pair,
-- :mod:`precis_se.motor`).
--
-- * `stator count` (count) — number of torque-generating stator units.
-- * `proton motive force` (mV) — the PMF input driving the motor.
-- * `stall torque` (N*m) — torque at zero speed.
-- * `zero load speed` (rad/s) — speed at zero torque.
-- * `operating speed` (rad/s) — the query point.
-- * `output torque` (N*m) — the computed linear torque-speed value
--   (`torque(w) = stall_torque * (1 - w / zero_load_speed)`), the one
--   measurand `precis_se.motor.MOTOR_COMPUTERS` realises.
--
-- Same shape as 0182_se_measurand_seed.sql / 0231: INSERTs only missing
-- nodes (a seed row is skipped when a live taxon with the same slug
-- already sits under the `measurand` start node), every node lands
-- status='proposed', one ord -1 `card_combined` chunk with no embedding,
-- one `specialises` link to the `measurand` start node. NO `measurand`
-- start node → no-op.
--
-- SI vector order: m, kg, s, A, K, mol, cd. The se unit snapshots to
-- canonical_unit (precis_se.properties.measurand unit rule 3);
-- dimensionless/count nodes carry no canonical_unit si_vector (rules
-- 3-4).
--
-- Forward-only (ADR 0005). Squawk: INSERTs only, no DDL on live tables.

BEGIN;

CREATE TEMP TABLE _measurand_seed ON COMMIT DROP AS
SELECT v.ord,
       v.name,
       trim(both '-' from regexp_replace(lower(v.name), '[^a-z0-9]+', '-', 'g'))
           AS slug,
       jsonb_strip_nulls(jsonb_build_object(
           'name', v.name,
           'norm_name', lower(regexp_replace(v.name, '\s+', ' ', 'g')),
           'slug', trim(both '-' from
                        regexp_replace(lower(v.name), '[^a-z0-9]+', '-', 'g')),
           'definition', v.definition,
           'aliases', '[]'::jsonb,
           'status', 'proposed',
           'dimension_kind', v.dkind,
           'si_vector', v.si_vector,
           'canonical_unit', v.unit,
           'value_type', v.value_type,
           'allowed_values', v.allowed
       )) AS meta
FROM (VALUES
    (1, 'Stator count',
     'Stator count is the number of torque-generating stator units a rotary biomolecular motor presents.',
     'count', NULL, 'count', 'quantity', NULL::jsonb),
    (2, 'Proton motive force',
     'Proton motive force is the transmembrane electrochemical potential driving a rotary biomolecular motor, in millivolts.',
     'si', '2,1,-3,-1,0,0,0', 'mV', 'quantity', NULL),
    (3, 'Stall torque',
     'Stall torque is the torque a rotary motor delivers at zero rotational speed.',
     'si', '2,1,-2,0,0,0,0', 'N*m', 'quantity', NULL),
    (4, 'Zero load speed',
     'Zero load speed is the rotational speed a motor reaches when it delivers zero torque.',
     'si', '0,0,-1,0,0,0,0', 'rad/s', 'quantity', NULL),
    (5, 'Operating speed',
     'Operating speed is the rotational speed a motor is queried at.',
     'si', '0,0,-1,0,0,0,0', 'rad/s', 'quantity', NULL),
    (6, 'Output torque',
     'Output torque is the torque a rotary motor delivers at its declared operating speed, on the linear torque-speed curve.',
     'si', '2,1,-2,0,0,0,0', 'N*m', 'quantity', NULL)
) AS v (ord, name, definition, dkind, si_vector, unit, value_type, allowed);

CREATE TEMP TABLE _measurand_have ON COMMIT DROP AS
WITH RECURSIVE root AS (
    SELECT ref_id FROM refs
     WHERE kind = 'taxon' AND retired_at IS NULL
       AND meta->>'start' = 'true' AND meta->>'slug' = 'measurand'
),
under (ref_id) AS (
    SELECT ref_id FROM root
    UNION
    SELECT l.src_ref_id
      FROM links l
      JOIN under u ON l.dst_ref_id = u.ref_id
      JOIN refs c ON c.ref_id = l.src_ref_id
                 AND c.kind = 'taxon' AND c.retired_at IS NULL
     WHERE l.relation = 'specialises'
       AND l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL
)
SELECT DISTINCT r.meta->>'slug' AS slug
  FROM under u JOIN refs r ON r.ref_id = u.ref_id;

WITH root AS (
    SELECT ref_id FROM refs
     WHERE kind = 'taxon' AND retired_at IS NULL
       AND meta->>'start' = 'true' AND meta->>'slug' = 'measurand'
     ORDER BY ref_id
     LIMIT 1
),
ins AS (
    INSERT INTO refs (kind, title, meta)
    SELECT 'taxon', s.name, s.meta
      FROM _measurand_seed s
     WHERE NOT EXISTS (SELECT 1 FROM _measurand_have h WHERE h.slug = s.slug)
       AND EXISTS (SELECT 1 FROM root)
     ORDER BY s.ord
    RETURNING ref_id, meta
),
card AS (
    INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta)
    SELECT ins.ref_id, -1, 'card_combined',
           (ins.meta->>'name') || ' — ' || (ins.meta->>'definition'),
           '{}'::jsonb
      FROM ins
    RETURNING ref_id
)
INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by)
SELECT ins.ref_id, root.ref_id, 'specialises', 'system'
  FROM ins CROSS JOIN root;

COMMIT;

-- End of 0268_se_motor_measurands.sql
