-- 0093_component_kind.sql
--
-- The `component` kind (docs/proposals/component-kind.md) — a general
-- procurable-part store: bolts, hoses, pipes, beams, gaskets, bearings,
-- adhesives, electronic components. Unlike `material` (a bulk substance and
-- its intensive properties, ADR 0070), a component is a **discrete
-- procurable thing** with extensive facts (mpn, per-unit cost, overall
-- length, pressure rating) that is **made of** a material. Distinct from
-- `part` (the JLCPCB/LCSC ingest-only catalog, addressed by C-number) — see
-- ADR 0070 "Relation to `part`".
--
-- Mirrors `material`'s star schema (0092) column-for-column, plus a
-- category dimension:
--
--   * The COMPONENT ENTITY reuses `refs` (kind='component', slug id):
--     `title` = canonical name; `meta` = {category, mpn, manufacturer, sku,
--     uom, package, aliases, notes}.
--   * `component_categories` — a growable, FLAT (no taxonomy tree) category
--     registry (`core`/`proposed` tier). Seeded with a curated `core`
--     starter set below; an unknown `category=` on an entity write mints a
--     fresh `proposed` category (handler-enforced, never silently `core`).
--   * `component_specs` — a typed, growable spec registry, the direct
--     analogue of `material_properties`, PLUS a nullable `category_id` FK:
--     non-null = the spec belongs to that category; NULL = universal
--     (applies to any component, e.g. `mass`, `unit_cost`,
--     `length_overall`). A value write for `spec=S` on a component in
--     category `C` is accepted only if `S.category_id IS NULL OR
--     S.category_id = C` — handler-enforced applicability, not a DB
--     constraint (the check needs the component's category, a join away).
--   * `component_spec_values` — the fact table, one row per sourced
--     measurement, the `material_values` shape verbatim
--     (`component_ref_id` in place of `material_ref_id`, `spec_id` in
--     place of `property_id`). `component_ref_id` has no per-kind FK (refs
--     has no per-kind check) — the handler enforces `kind='component'` at
--     write time. `input_unit` is reserved, always NULL in v1 (the shared
--     unit-conversion follow-on's write column, same as material's).
--   * The `made-of` / `used-in` relation pair (component → material) — the
--     one composition edge in v1; see `store/types.py`'s `Relation` Literal
--     + `_INVERSE_RELATIONS` map, kept in sync with this seed.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

-- 1. the ref kind ------------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('component', FALSE, 'Component',
     'General procurable-part store — a slug entity (name/category/mpn/'
     'manufacturer) plus per-spec sourced values in a typed, growable, '
     'category-scoped spec registry. made-of links a component to the '
     'material it is made of. v1 is canonical-units-only, like material. '
     'See precis-component-help.')
ON CONFLICT (slug) DO NOTHING;

-- 2. the made-of / used-in relation pair --------------------------------
INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('made-of', FALSE, 'used-in',
     'Source component is made of target material.'),
    ('used-in', FALSE, 'made-of',
     'Source material is used in target component.')
ON CONFLICT (slug) DO NOTHING;

-- 3. the category registry ----------------------------------------------
CREATE TABLE IF NOT EXISTS component_categories (
    category_id  text PRIMARY KEY,
    name         text NOT NULL,
    status       text NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('core', 'proposed')),
    description  text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE component_categories IS
    'Growable, flat (no taxonomy tree) component-category registry '
    '(component-kind proposal). core = curated starter set; proposed = '
    'minted at entity-write time, never silently promoted.';

-- 4. the spec registry ---------------------------------------------------
CREATE TABLE IF NOT EXISTS component_specs (
    spec_id          text PRIMARY KEY,
    name             text NOT NULL,
    canonical_unit   text,               -- NULL = dimensionless/categorical
    dimension        text,               -- quantity-kind label (descriptive, v1)
    value_type       text NOT NULL
        CHECK (value_type IN ('quantity', 'ratio', 'categorical', 'boolean', 'text')),
    allowed_values   jsonb,              -- closed set for categoricals; else NULL
    standard_ref     text,               -- ASTM/ISO/IUPAC/QUDT URI for the spec itself
    status           text NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('core', 'proposed')),
    higher_is_better boolean,            -- reserved for later selection use
    description      text,
    category_id      text REFERENCES component_categories (category_id),
                                          -- NULL = universal (applies to any component)
    created_at       timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE component_specs IS
    'Typed, growable, category-scoped component-spec registry '
    '(component-kind proposal). category_id IS NULL = universal '
    '(mass/unit_cost/length_overall); non-NULL = scoped to that category, '
    'handler-enforced at write time. core = curated starter set; proposed '
    '= minted at write time (must declare a canonical unit + dimension), '
    'never silently promoted.';

-- 5. the value fact table -------------------------------------------------
CREATE TABLE IF NOT EXISTS component_spec_values (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    component_ref_id bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    spec_id          text NOT NULL REFERENCES component_specs (spec_id),
    value_num        double precision,   -- in the spec's canonical unit
    value_low        double precision,   -- range / uncertainty lower bound
    value_high       double precision,   -- range / uncertainty upper bound
    value_text       text,               -- categorical / text value
    value_bool       boolean,
    input_unit       text,               -- reserved for the unit-conversion
                                          -- follow-on; always NULL in v1
    conditions       jsonb NOT NULL DEFAULT '{}'::jsonb,
    maturity         text NOT NULL DEFAULT 'lab'
        CHECK (maturity IN ('commercial', 'lab', 'speculative')),
    method           text,               -- measured | datasheet | estimated | ...
    source_ref_id    bigint REFERENCES refs (ref_id) ON DELETE SET NULL,
    source_chunk     text,               -- chunk handle into source_ref_id
    source_url       text,               -- fallback source, no ref
    as_of            date,               -- load-bearing for unit_cost etc.
    set_by           text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    notes            text
);

COMMENT ON TABLE component_spec_values IS
    'component-kind proposal: one sourced measurement per row. '
    'component_ref_id is handler-enforced to kind=component (refs has no '
    'per-kind FK). value_num is stored in the spec''s canonical unit '
    '(v1 does no conversion). Per-unit cost is just the universal '
    'unit_cost spec, paired with as_of + conditions={qty_break}.';

CREATE INDEX IF NOT EXISTS component_spec_values_spec_value_idx
    ON component_spec_values (spec_id, value_num);
CREATE INDEX IF NOT EXISTS component_spec_values_component_idx
    ON component_spec_values (component_ref_id);

-- 6. core category seed set ----------------------------------------------
INSERT INTO component_categories (category_id, name, status, description)
VALUES
    ('fastener', 'Fastener', 'core',
     'Bolts, screws, nuts, washers, rivets.'),
    ('hose', 'Hose', 'core', 'Flexible fluid/gas conveyance.'),
    ('pipe', 'Pipe', 'core', 'Rigid fluid/gas conveyance.'),
    ('profile', 'Profile', 'core', 'Structural beams / extrusions.'),
    ('electronic', 'Electronic', 'core',
     'Discrete or module-level electronic components.'),
    ('adhesive', 'Adhesive', 'core', 'Bonding compounds / tapes.'),
    ('seal', 'Seal', 'core', 'Gaskets, o-rings, packings.'),
    ('bearing', 'Bearing', 'core', 'Ball/roller/plain bearings, bushings.'),
    ('fitting', 'Fitting', 'core', 'Pipe/hose connectors, adapters, unions.'),
    ('laminate', 'Laminate', 'core',
     'Layered composite sheet/panel material (measured specs only in v1 - '
     'no layer-structure model, see the proposal''s deferrals).')
ON CONFLICT (category_id) DO NOTHING;

-- 7. universal spec seed (category_id NULL) -------------------------------
INSERT INTO component_specs
    (spec_id, name, canonical_unit, dimension, value_type, status, description)
VALUES
    ('mass', 'Mass', 'kg', 'mass', 'quantity', 'core',
     'Mass of one unit of the component.'),
    ('unit_cost', 'Unit cost', 'USD', 'currency', 'quantity', 'core',
     'Cost per unit (each/m/kg/... per uom=); pair with as_of and, for '
     'price breaks, conditions={"qty_break": 100}.'),
    ('length_overall', 'Overall length', 'm', 'length', 'quantity', 'core',
     'Overall length of one unit of the component.')
ON CONFLICT (spec_id) DO NOTHING;

-- 8. curated core spec seed per category -----------------------------------
INSERT INTO component_specs
    (spec_id, name, canonical_unit, dimension, value_type, allowed_values,
     status, category_id, description)
VALUES
    -- fastener
    ('thread_size', 'Thread size', NULL, 'categorical', 'categorical',
     '["M3", "M4", "M5", "M6", "M8", "M10", "M12", "M16", "M20"]'::jsonb,
     'core', 'fastener', 'Nominal metric thread designation.'),
    ('thread_pitch', 'Thread pitch', 'mm', 'length', 'quantity', NULL,
     'core', 'fastener', 'Distance between adjacent thread crests.'),
    ('length', 'Length', 'mm', 'length', 'quantity', NULL,
     'core', 'fastener', 'Fastener shank/overall length.'),
    ('grade', 'Grade', NULL, 'categorical', 'categorical',
     '["4.8", "8.8", "10.9", "12.9", "A2", "A4"]'::jsonb,
     'core', 'fastener', 'Strength/corrosion-resistance class marking.'),
    ('drive_type', 'Drive type', NULL, 'categorical', 'categorical',
     '["hex", "socket", "phillips", "slotted", "torx", "allen"]'::jsonb,
     'core', 'fastener', 'Tool interface for driving the fastener.'),
    -- hose
    ('bore_diameter', 'Bore diameter', 'mm', 'length', 'quantity', NULL,
     'core', 'hose', 'Inner (through-bore) diameter.'),
    ('max_working_pressure', 'Maximum working pressure', 'MPa',
     'pressure/stress', 'quantity', NULL,
     'core', 'hose', 'Rated continuous working pressure.'),
    ('min_bend_radius', 'Minimum bend radius', 'mm', 'length', 'quantity',
     NULL, 'core', 'hose', 'Smallest radius the hose may be bent to '
     'without kinking/damage.'),
    ('temperature_max', 'Maximum service temperature', 'K', 'temperature',
     'quantity', NULL, 'core', 'hose',
     'Upper continuous-use temperature. Absolute scale (Kelvin).'),
    -- bearing
    ('bore_diameter_bearing', 'Bore diameter', 'mm', 'length', 'quantity',
     NULL, 'core', 'bearing', 'Inner-ring bore diameter.'),
    ('dynamic_load_rating', 'Dynamic load rating', 'N', 'force', 'quantity',
     NULL, 'core', 'bearing',
     'Basic dynamic load rating (rated fatigue life at constant load).')
ON CONFLICT (spec_id) DO NOTHING;

-- 9. proposed-tier examples (exercise the tiering; never silently core) ---
INSERT INTO component_specs
    (spec_id, name, canonical_unit, dimension, value_type, allowed_values,
     status, category_id, description)
VALUES
    ('finish', 'Surface finish', NULL, 'categorical', 'categorical',
     '["plain", "zinc-plated", "black-oxide", "anodized"]'::jsonb,
     'proposed', 'fastener', 'Surface treatment/coating.'),
    ('is_reinforced', 'Is reinforced', NULL, 'categorical', 'boolean', NULL,
     'proposed', 'hose', 'Whether the hose carries a reinforcing braid/wire.')
ON CONFLICT (spec_id) DO NOTHING;

COMMIT;

-- End of 0093_component_kind.sql
