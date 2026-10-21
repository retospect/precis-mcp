-- 0228_se_measurands_esp_binding.sql
--
-- Seeds the two measurands pocket-from-molecule slice 2 needs
-- (docs/backlog/pocket-from-molecule.md, readiness advisory 2026-10-20):
--
-- * `electrostatic potential` (V) — the requirement a pocket region
--   puts on the wall: the mirrored ESP band of a molecule surface
--   patch (precis_se.properties.pocket_spec).
-- * `binding energy` (eV) — the candidate score slice 3's held-shape
--   spec and slice 4's realize loop compare; seeded now so both slices
--   write against one node.
--
-- Same shape as 0182_se_measurand_seed.sql: INSERTs only missing nodes
-- (a seed row is skipped when a live taxon with the same slug already
-- sits under the `measurand` start node), every node lands
-- status='proposed', one ord -1 `card_combined` chunk with no embedding,
-- one `specialises` link to the `measurand` start node. NO `measurand`
-- start node → no-op.
--
-- SI vector order: m, kg, s, A, K, mol, cd. The se unit snapshots to
-- canonical_unit for both (precis_se.properties.measurand unit rule 3).
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
    (1, 'Electrostatic potential',
     'Electrostatic potential is the electric potential a charge distribution produces at a point on or near a surface, in volts.',
     'si', '2,1,-3,-1,0,0,0', 'V', 'quantity', NULL::jsonb),
    (2, 'Binding energy',
     'Binding energy is the energy released when two bodies bind, the stabilisation a pocket must provide, in electronvolts.',
     'si', '2,1,-2,0,0,0,0', 'eV', 'quantity', NULL)
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

-- End of 0228_se_measurands_esp_binding.sql
