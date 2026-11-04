-- 0274_se_flow_measurands.sql
--
-- Seeds the measurands precis_se.flow.FLOW_COMPUTERS realise, so a flow
-- measure can name its measurand through add_measure (se-flow-kernel
-- slice 4a: the workspace import's `out.physics` measures). All eight
-- are reduced numbers, which is why they carry no unit:
--
-- * `breakthrough fraction` — share of tracer particles that cross the
--   tracking plane.
-- * `residence time t0` — mean particle transit in units of t0 = L/U0.
-- * `resistance ratio` — resistance over a smooth reference: the same
--   footprint with a flat floor, or for a lumen a smooth cylinder.
-- * `evenness` — 1 minus the nonuniformity of first crossings over a
--   region.
-- * `outlet conversion` / `outlet concentration` / `wall flux fraction`
--   / `damkohler` — the species outputs. 0231 seeded these; they are
--   listed so this file covers the whole table, and the skip below
--   leaves those rows alone.
--
-- Same shape as 0182_se_measurand_seed.sql / 0231 / 0271: INSERTs only
-- missing nodes (a seed row is skipped when a live taxon with the same
-- slug already sits under the `measurand` start node), every node lands
-- status='proposed', one ord -1 `card_combined` chunk with no embedding,
-- one `specialises` link to the `measurand` start node. NO `measurand`
-- start node → no-op.
--
-- Dimensionless nodes carry no canonical_unit and no si_vector → 'ratio'
-- (precis_se.properties.measurand unit rule 4).
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
    (1, 'Breakthrough fraction',
     'Breakthrough fraction is the share of tracer particles that cross a flow chamber''s tracking plane.',
     'dimensionless', NULL::text, NULL::text, 'ratio', NULL::jsonb),
    (2, 'Residence time t0',
     'Residence time t0 is the mean particle transit time through a flow chamber, in units of one transit t0 = L/U0.',
     'dimensionless', NULL, NULL, 'ratio', NULL),
    (3, 'Resistance ratio',
     'Resistance ratio is a flow passage''s resistance over that of its smooth reference: the same footprint with a flat floor for a chamber, a smooth cylinder of the effective radius for a lumen.',
     'dimensionless', NULL, NULL, 'ratio', NULL),
    (4, 'Evenness',
     'Evenness is one minus the nonuniformity of first particle crossings over a region of a flow chamber.',
     'dimensionless', NULL, NULL, 'ratio', NULL),
    (5, 'Outlet conversion',
     'Outlet conversion is the fraction of the inlet species flux consumed before the chamber outlet.',
     'dimensionless', NULL, NULL, 'ratio', NULL),
    (6, 'Outlet concentration',
     'Outlet concentration is the flux-weighted mean species concentration at the chamber outlet, relative to the inlet concentration.',
     'dimensionless', NULL, NULL, 'ratio', NULL),
    (7, 'Wall flux fraction',
     'Wall flux fraction is the share of the inlet species flux consumed on one reactive wall patch.',
     'dimensionless', NULL, NULL, 'ratio', NULL),
    (8, 'Damkohler',
     'Damkohler is the transit Damkohler number Da_I = n_w k_s L/(h0 u0), the wall consumption rate relative to convective throughput.',
     'dimensionless', NULL, NULL, 'ratio', NULL)
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

-- End of 0274_se_flow_measurands.sql
