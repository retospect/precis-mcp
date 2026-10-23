-- 0231_se_species_measurands.sql
--
-- Seeds the measurands se-multiphysics.md slice 1 needs (species
-- advection–diffusion + wall reactions on the thin-gap kernel):
--
-- * `inlet concentration` (mol/m^3) — declared inlet c_in on the inlet
--   patch (optional while the equations are linear).
-- * `wall rate constant` (m/s) — the first-order k_s a floor/lid patch
--   presents; written by the `wall_rate` op from a trusted catpath TOF,
--   or typed in directly with origin='user'.
-- * `species diffusivity` (m^2/s) — molecular D on a patch, falling
--   back to the fluid material's diffusivity.
-- * `inlet velocity` (m/s) — the operating point u0 (a patch measure:
--   BCs are measures, the flow: token names roles only).
-- * `pressure drop` (Pa) — the alternative operating point (u0 =
--   h0^2·dp/(12μL), needs the fluid's viscosity).
-- * `outlet conversion` / `outlet concentration` / `wall flux
--   fraction` / `damkohler` — the four slice-1 output measurands
--   (dimensionless) the species FLOW_COMPUTERS realise.
--
-- Same shape as 0182_se_measurand_seed.sql / 0228: INSERTs only missing
-- nodes (a seed row is skipped when a live taxon with the same slug
-- already sits under the `measurand` start node), every node lands
-- status='proposed', one ord -1 `card_combined` chunk with no embedding,
-- one `specialises` link to the `measurand` start node. NO `measurand`
-- start node → no-op.
--
-- SI vector order: m, kg, s, A, K, mol, cd. The se unit snapshots to
-- canonical_unit (precis_se.properties.measurand unit rule 3);
-- dimensionless nodes carry no canonical_unit → 'ratio' (rule 4).
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
    (1, 'Inlet concentration',
     'Inlet concentration is the species concentration a flow chamber inlet presents, in moles per cubic metre.',
     'si', '-3,0,0,0,0,1,0', 'mol/m^3', 'quantity', NULL::jsonb),
    (2, 'Wall rate constant',
     'Wall rate constant is the first-order heterogeneous rate constant a reactive wall patch presents, in metres per second.',
     'si', '1,0,-1,0,0,0,0', 'm/s', 'quantity', NULL::jsonb),
    (3, 'Species diffusivity',
     'Species diffusivity is the molecular diffusion coefficient of a transported species in the chamber fluid, in square metres per second.',
     'si', '2,0,-1,0,0,0,0', 'm^2/s', 'quantity', NULL::jsonb),
    (4, 'Inlet velocity',
     'Inlet velocity is the depth-averaged fluid speed a flow chamber inlet operates at, in metres per second.',
     'si', '1,0,-1,0,0,0,0', 'm/s', 'quantity', NULL::jsonb),
    (5, 'Pressure drop',
     'Pressure drop is the inlet-to-outlet pressure difference driving a thin-chamber flow, in pascals.',
     'si', '-1,1,-2,0,0,0,0', 'Pa', 'quantity', NULL::jsonb),
    (6, 'Outlet conversion',
     'Outlet conversion is the fraction of the inlet species flux consumed before the chamber outlet.',
     'dimensionless', NULL, NULL, 'ratio', NULL),
    (7, 'Outlet concentration',
     'Outlet concentration is the flux-weighted mean species concentration at the chamber outlet, relative to the inlet concentration.',
     'dimensionless', NULL, NULL, 'ratio', NULL),
    (8, 'Wall flux fraction',
     'Wall flux fraction is the share of the inlet species flux consumed on one reactive wall patch.',
     'dimensionless', NULL, NULL, 'ratio', NULL),
    (9, 'Damkohler',
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

-- End of 0231_se_species_measurands.sql
