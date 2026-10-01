-- 0174_taxon_seed.sql
--
-- Seeds the term taxonomy (docs/backlog/term-taxonomy.md, stage D, AC 7):
-- the two start nodes `measurand` and `subject`, plus ONE `taxon` per live
-- row of the four legacy registries, INSERT ... SELECT over the tables as
-- they stand (the migrations seeded fewer rows than prod holds, so there is
-- no hand-written list here):
--
--   material_properties, component_specs, rxn_properties -> specialises measurand
--   component_categories                                 -> specialises subject
--
-- Rows are NOT merged across registries (`max_service_temperature` vs
-- `temperature_max` stay two nodes): node count = legacy row count + 2.
-- Every node lands status='proposed' — `core` was a curation marker, not a
-- usage measurement; `systematic` is earned later. The legacy tables are
-- read, never changed.
--
-- Node shape is byte-identical to a hand-put node (precis.taxonomy.nodes):
--   refs.title = name; meta = {name, norm_name, slug, definition, aliases,
--   status, dimension_kind?, si_vector?, canonical_unit?, value_type?,
--   allowed_values?, standard_ref?, higher_is_better?, legacy_source,
--   applies_to_ref?}; one ord -1 `card_combined` chunk with
--   taxon_card_text(name, definition, aliases) and no embedding (the embed
--   worker picks it up); one `specialises` link child -> start node.
--   norm_name / slug replicate precis.reading.concepts.normalize_name and
--   precis.taxonomy.nodes.slugify in SQL (tests/test_taxon.py::TestSeed
--   compares them against the Python for every seeded row).
--
-- DIMENSION. The legacy `dimension` label maps through the VALUES lookup
-- below to (dimension_kind, si_vector). The vector is the seven SI base
-- exponents in the order m, kg, s, A, K, mol, cd. A row with a NULL
-- canonical_unit takes (categorical | dimensionless) from its value_type
-- (categorical/boolean/text -> categorical; quantity/ratio -> dimensionless)
-- instead of being refused. A label with no lookup row seeds
-- dimension_kind NULL (the migration does not fail on it) and is listed by
-- `get(kind='taxon', id='/unmapped')`.
--
-- `applies_to_ref` (component_specs.category_id) is a meta pointer ('tn<id>')
-- to the seeded subject node of that category — NOT a specialises edge.
--
-- Idempotent: nodes are keyed on meta.legacy_source = {table, key} (start
-- nodes on meta.start + slug), so replaying the file inserts nothing new.
-- The closing DO block asserts per-table count equality against the live
-- legacy tables and RAISEs (rolling the whole file back) on a mismatch.
--
-- Forward-only (ADR 0005). Squawk: INSERTs only, no DDL on live tables.

BEGIN;

-- Hold off legacy writes for the file's runtime so step 6's count check
-- cannot race a concurrent registry insert from the still-serving release.
LOCK TABLE material_properties, component_specs, rxn_properties,
           component_categories IN SHARE MODE;

-- 1. start nodes ---------------------------------------------------------
INSERT INTO refs (kind, title, meta)
SELECT 'taxon', s.name,
       jsonb_build_object(
           'name', s.name,
           'norm_name', lower(s.name),
           'slug', s.name,
           'definition', s.definition,
           'aliases', '[]'::jsonb,
           'status', 'proposed',
           'start', TRUE,
           'contract', jsonb_build_object('required_keys', s.required_keys)
       )
FROM (VALUES
    ('measurand',
     'A measurand is a quantity, property or observable that can be measured or computed for a thing, such as a density, a melting temperature or a reaction yield.',
     '["dimension_kind"]'::jsonb),
    ('subject',
     'A subject is a thing or a class of things about which properties are stated, such as a material, a component category or a reaction.',
     '[]'::jsonb)
) AS s (name, definition, required_keys)
WHERE NOT EXISTS (
    SELECT 1 FROM refs r
     WHERE r.kind = 'taxon'
       AND r.meta->>'start' = 'true'
       AND r.meta->>'slug' = s.name
);

-- 2. stage the legacy rows (computed once, joined by every step below) ----
CREATE TEMP TABLE _taxon_seed ON COMMIT DROP AS
WITH dim_lookup (label, kind, si_vector) AS (
    -- legacy `dimension` label -> (dimension_kind, canonical si_vector).
    -- Vector order: m, kg, s, A, K, mol, cd. Unit-style labels (Å, nm, s)
    -- map to the quantity they measure. Labels absent from this list seed
    -- dimension_kind NULL and show up in /unmapped.
    VALUES
    ('length',                           'si',          '1,0,0,0,0,0,0'),
    ('Å',                                'si',          '1,0,0,0,0,0,0'),
    ('nm',                               'si',          '1,0,0,0,0,0,0'),
    ('mass',                             'si',          '0,1,0,0,0,0,0'),
    ('time',                             'si',          '0,0,1,0,0,0,0'),
    ('s',                                'si',          '0,0,1,0,0,0,0'),
    ('temperature',                      'si',          '0,0,0,0,1,0,0'),
    ('1/temperature',                    'si',          '0,0,0,0,-1,0,0'),
    ('amount',                           'si',          '0,0,0,0,0,1,0'),
    ('mass/volume',                      'si',          '-3,1,0,0,0,0,0'),
    ('force',                            'si',          '1,1,-2,0,0,0,0'),
    ('pressure',                         'si',          '-1,1,-2,0,0,0,0'),
    ('pressure/stress',                  'si',          '-1,1,-2,0,0,0,0'),
    ('energy/(mass*temperature)',        'si',          '2,0,-2,0,-1,0,0'),
    ('power/(length*temperature)',       'si',          '1,1,-3,0,-1,0,0'),
    ('resistance*length',                'si',          '3,1,-3,-2,0,0,0'),
    ('voltage/length',                   'si',          '1,1,-3,-1,0,0,0'),
    ('angle',                            'dimensionless', NULL),
    ('dimensionless',                    'dimensionless', NULL),
    ('currency',                         'currency',    NULL),
    ('currency/mass',                    'currency',    NULL),
    ('hardness (non-convertible scale)', 'scale',       NULL),
    ('categorical',                      'categorical', NULL),
    ('text',                             'categorical', NULL)
),
legacy AS (
    SELECT 'material_properties' AS tbl, prop_id AS key, name, canonical_unit,
           dimension, value_type, allowed_values, standard_ref,
           higher_is_better, description, NULL::text AS category_key,
           'measurand' AS parent_slug
      FROM material_properties
    UNION ALL
    SELECT 'component_specs', spec_id, name, canonical_unit, dimension,
           value_type, allowed_values, standard_ref, higher_is_better,
           description, category_id, 'measurand'
      FROM component_specs
    UNION ALL
    SELECT 'rxn_properties', prop_id, name, canonical_unit, dimension,
           value_type, allowed_values, standard_ref, higher_is_better,
           description, NULL, 'measurand'
      FROM rxn_properties
    UNION ALL
    SELECT 'component_categories', category_id, name, NULL, NULL, NULL, NULL,
           NULL, NULL, description, NULL, 'subject'
      FROM component_categories
),
named AS (
    SELECT l.*,
           regexp_replace(l.name, '^\s+|\s+$', '', 'g') AS nm,
           regexp_replace(COALESCE(l.description, ''), '^\s+|\s+$', '', 'g') AS descr
      FROM legacy l
),
dimmed AS (
    SELECT n.*,
           CASE
               WHEN n.tbl = 'component_categories' THEN NULL
               WHEN n.canonical_unit IS NULL THEN
                   CASE WHEN n.value_type IN ('categorical', 'boolean', 'text')
                        THEN 'categorical' ELSE 'dimensionless' END
               ELSE d.kind
           END AS dkind,
           d.si_vector AS lookup_vec
      FROM named n
      LEFT JOIN dim_lookup d ON d.label = n.dimension
)
SELECT
    m.tbl,
    m.key,
    m.parent_slug,
    m.category_key,
    jsonb_build_object('table', m.tbl, 'key', m.key) AS legacy_source,
    m.nm AS name,
    jsonb_strip_nulls(jsonb_build_object(
        'name', m.nm,
        'norm_name', lower(regexp_replace(m.nm, '\s+', ' ', 'g')),
        'slug', trim(both '-' from regexp_replace(lower(m.nm), '[^a-z0-9]+', '-', 'g')),
        'definition', m.definition,
        'aliases', CASE WHEN lower(regexp_replace(m.nm, '\s+', ' ', 'g')) <> lower(m.key)
                        THEN jsonb_build_array(m.key) ELSE '[]'::jsonb END,
        'status', 'proposed',
        'dimension_kind', m.dkind,
        'si_vector', CASE WHEN m.dkind = 'si' THEN m.lookup_vec END,
        'canonical_unit', m.canonical_unit,
        'value_type', m.value_type,
        'allowed_values', m.allowed_values,
        'standard_ref', m.standard_ref,
        'higher_is_better', m.higher_is_better,
        'legacy_source', jsonb_build_object('table', m.tbl, 'key', m.key)
    )) AS meta
FROM (
    SELECT d.*,
           CASE
               WHEN d.descr <> '' THEN d.descr
               WHEN d.tbl = 'component_categories' THEN format(
                   '%s: a component category, carried over from the legacy component_categories registry, which recorded no fuller definition.',
                   d.nm)
               ELSE format(
                   '%s: a %s term%s, carried over from the legacy %s registry, which recorded no fuller definition.',
                   d.nm, d.value_type,
                   CASE WHEN d.canonical_unit IS NOT NULL
                        THEN ' measured in ' || d.canonical_unit ELSE '' END,
                   d.tbl)
           END AS definition
      FROM dimmed d
) m;

-- 3. nodes: categories first (specs point at them), then the rest --------
INSERT INTO refs (kind, title, meta)
SELECT 'taxon', s.name, s.meta
  FROM _taxon_seed s
 WHERE s.tbl = 'component_categories'
   AND NOT EXISTS (
       SELECT 1 FROM refs r
        WHERE r.kind = 'taxon' AND r.meta->'legacy_source' = s.legacy_source)
 ORDER BY s.key;

INSERT INTO refs (kind, title, meta)
SELECT 'taxon', s.name,
       s.meta || CASE WHEN cat.ref_id IS NOT NULL
                      THEN jsonb_build_object('applies_to_ref', 'tn' || cat.ref_id)
                      ELSE '{}'::jsonb END
  FROM _taxon_seed s
  LEFT JOIN refs cat
    ON cat.kind = 'taxon'
   AND s.category_key IS NOT NULL
   AND cat.meta->'legacy_source' = jsonb_build_object(
           'table', 'component_categories', 'key', s.category_key)
 WHERE s.tbl <> 'component_categories'
   AND NOT EXISTS (
       SELECT 1 FROM refs r
        WHERE r.kind = 'taxon' AND r.meta->'legacy_source' = s.legacy_source)
 ORDER BY s.tbl, s.key;

-- 4. the ord -1 card_combined chunk (embedding NULL: the worker embeds) ---
-- text = precis.taxonomy.nodes.taxon_card_text(name, definition, aliases)
INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta)
SELECT r.ref_id, -1, 'card_combined',
       (r.meta->>'name')
       || CASE WHEN r.meta->>'definition' <> ''
               THEN ' — ' || (r.meta->>'definition') ELSE '' END
       || COALESCE((
              SELECT ' (aka ' || string_agg(a.alias, ', ' ORDER BY a.n) || ')'
                FROM jsonb_array_elements_text(r.meta->'aliases')
                     WITH ORDINALITY AS a (alias, n)
           ), ''),
       '{}'::jsonb
  FROM refs r
 WHERE r.kind = 'taxon'
   AND (r.meta ? 'legacy_source'
        OR (r.meta->>'start' = 'true' AND r.meta->>'slug' IN ('measurand', 'subject')))
   AND NOT EXISTS (
       SELECT 1 FROM chunks c WHERE c.ref_id = r.ref_id AND c.ord = -1);

-- 5. position: child specialises its start node --------------------------
INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by)
SELECT child.ref_id, parent.ref_id, 'specialises', 'system'
  FROM _taxon_seed s
  JOIN refs child
    ON child.kind = 'taxon' AND child.meta->'legacy_source' = s.legacy_source
  JOIN refs parent
    ON parent.kind = 'taxon'
   AND parent.meta->>'start' = 'true'
   AND parent.meta->>'slug' = s.parent_slug
 WHERE NOT EXISTS (
       SELECT 1 FROM links l
        WHERE l.src_ref_id = child.ref_id AND l.dst_ref_id = parent.ref_id
          AND l.relation = 'specialises'
          AND l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL);

-- 6. count assertion against the live legacy tables ----------------------
DO $$
DECLARE
    t text;
    n_legacy bigint;
    n_nodes bigint;
BEGIN
    FOREACH t IN ARRAY ARRAY['material_properties', 'component_specs',
                             'rxn_properties', 'component_categories']
    LOOP
        EXECUTE format('SELECT count(*) FROM %I', t) INTO n_legacy;
        SELECT count(*) INTO n_nodes FROM refs
         WHERE kind = 'taxon' AND meta->'legacy_source'->>'table' = t;
        IF n_nodes <> n_legacy THEN
            RAISE EXCEPTION 'taxon seed: % has % rows but % taxon nodes',
                t, n_legacy, n_nodes;
        END IF;
    END LOOP;
END $$;

COMMIT;

-- End of 0174_taxon_seed.sql
