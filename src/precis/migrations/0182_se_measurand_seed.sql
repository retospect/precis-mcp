-- 0182_se_measurand_seed.sql
--
-- Seeds the `measurand` taxon nodes the se region property layer names
-- (docs/backlog/se-region-property-layer.md, slice A, in-scope 1): the
-- non-geometric properties a region measure declares — contact angle,
-- surface charge density, net partial charge, dipole moment, H-bond
-- donor/acceptor counts, electric field magnitude, absorption maximum
-- wavelength, hydrophobicity index — plus the four nodes se's legacy
-- `unit` enum maps onto (precis_se.measures.LEGACY_MEASURANDS: length,
-- count, ratio, angle). `Length` already exists wherever 0174 seeded the
-- component_specs registry; it is listed so a database without it still
-- gets one.
--
-- INSERTs ONLY MISSING NODES: a seed row is skipped when a live taxon
-- with the same slug already sits under the `measurand` start node
-- (any depth, over `specialises` edges). Replaying the file inserts
-- nothing. Every new node lands status='proposed' (term-taxonomy.md: a
-- status is earned).
--
-- Node shape is byte-identical to a hand-put node and to 0174's seed
-- (precis.taxonomy.nodes): refs.title = name; meta = {name, norm_name,
-- slug, definition, aliases, status, dimension_kind, si_vector?,
-- canonical_unit?, value_type, allowed_values?}; one ord -1
-- `card_combined` chunk = taxon_card_text(name, definition, aliases)
-- with no embedding (the embed worker picks it up); one `specialises`
-- link child -> the `measurand` start node. norm_name / slug replicate
-- precis.reading.concepts.normalize_name and precis.taxonomy.nodes.slugify
-- in SQL (tests/test_se_regions.py compares them against the Python).
--
-- NO `measurand` START NODE, NO-OP: every node needs that parent, so a
-- database without it (a truncated test DB the migrator catches up)
-- inserts nothing rather than minting unrooted nodes or failing the chain.
--
-- Units are what an se measure stores (se is float64 SI for lengths):
-- the wavelength is in m; charge in e and dipole in D are the units the
-- property is quoted in. SI vector order: m, kg, s, A, K, mol, cd.
--
-- Forward-only (ADR 0005). Squawk: INSERTs only, no DDL on live tables.

BEGIN;

-- 1. the seed rows ----------------------------------------------------------
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
    (1, 'Length',
     'Length is the extent of a thing or the distance between two features, measured along a line.',
     'si', '1,0,0,0,0,0,0', 'm', 'quantity', NULL::jsonb),
    (2, 'Angle',
     'Angle is the rotation between two lines or planes that meet, quoted in degrees.',
     'dimensionless', NULL, 'deg', 'quantity', NULL),
    (3, 'Count',
     'Count is the number of discrete items of one kind, such as gear teeth or binding sites.',
     'count', NULL, 'count', 'quantity', NULL),
    (4, 'Ratio',
     'Ratio is a dimensionless quotient of two quantities of the same dimension.',
     'dimensionless', NULL, NULL, 'ratio', NULL),
    (5, 'Contact angle',
     'Contact angle is the angle a liquid drop makes with a solid surface at the three-phase line, the measurable form of a surface''s hydrophobicity.',
     'dimensionless', NULL, 'deg', 'quantity', NULL),
    (6, 'Surface charge density',
     'Surface charge density is the net electric charge per unit area of a surface region.',
     'si', '-2,0,1,1,0,0,0', 'C/m^2', 'quantity', NULL),
    (7, 'Net partial charge',
     'Net partial charge is the summed partial atomic charge of a group of atoms, in units of the elementary charge.',
     'si', '0,0,1,1,0,0,0', 'e', 'quantity', NULL),
    (8, 'Dipole moment',
     'Dipole moment is the separation of positive and negative charge in a group of atoms, in debye.',
     'si', '1,0,1,1,0,0,0', 'D', 'quantity', NULL),
    (9, 'H-bond donor count',
     'H-bond donor count is the number of hydrogen-bond donor groups a region exposes.',
     'count', NULL, 'count', 'quantity', NULL),
    (10, 'H-bond acceptor count',
     'H-bond acceptor count is the number of hydrogen-bond acceptor groups a region exposes.',
     'count', NULL, 'count', 'quantity', NULL),
    (11, 'Electric field magnitude',
     'Electric field magnitude is the strength of the electric field at a point, in volts per metre.',
     'si', '1,1,-3,-1,0,0,0', 'V/m', 'quantity', NULL),
    (12, 'Absorption maximum wavelength',
     'Absorption maximum wavelength is the wavelength at which a chromophore absorbs most strongly, in metres.',
     'si', '1,0,0,0,0,0,0', 'm', 'quantity', NULL),
    (13, 'Hydrophobicity index',
     'Hydrophobicity index is a categorical rating of how strongly a surface region repels water, the form a designer states before any contact angle is known.',
     'categorical', NULL, NULL, 'categorical',
     '["hydrophilic", "amphiphilic", "hydrophobic"]'::jsonb)
) AS v (ord, name, definition, dkind, si_vector, unit, value_type, allowed);

-- 2. the slugs already under `measurand` (any depth, specialises edges) ----
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

-- 3. nodes, card chunks and positions: only the missing ones ----------------
-- One statement: the refs INSERT's RETURNING feeds the chunk and link
-- INSERTs through data-modifying CTEs, so no scratch table is written
-- (every INSERT target is a real, already-classified table).
-- The ord -1 card_combined chunk has embedding NULL: the worker embeds.
-- text = precis.taxonomy.nodes.taxon_card_text(name, definition, aliases);
-- every node here has no aliases. Position: child specialises the
-- measurand start node.
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

-- End of 0182_se_measurand_seed.sql
