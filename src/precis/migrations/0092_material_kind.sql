-- 0092_material_kind.sql
--
-- The `material` kind (docs/proposals/materials-handbook-kind.md) — a
-- CRC-handbook-style engineering material properties store, v1
-- **canonical-units-only** (no pint, no unit conversion, no off-sample
-- estimate/interpolation — both deferred follow-ons blocked-by this one).
--
-- A small star schema:
--
--   * The MATERIAL ENTITY reuses `refs` (kind='material', slug id):
--     `title` = canonical name; `meta` = {aliases, material_class,
--     composition/formula, notes}.
--   * `material_properties` — a typed, growable property registry (NOT a
--     frozen enum). Each row declares a canonical unit (nullable —
--     dimensionless/categorical), a dimension label (descriptive in v1;
--     the unit-conversion follow-on uses it for compat checks), a
--     value-type, and a `core`/`proposed` tier. Seeded with the curated
--     `core` starter set below (temperatures in Kelvin — absolute scale,
--     future-proof) plus two `proposed`-tier examples (`crystal_structure`
--     categorical, `is_magnetic` boolean) exercising the tiering.
--   * `material_values` — the fact table: one row per sourced measurement,
--     `(material, property, value, conditions, maturity, source)`. Multiple
--     rows per `(material, property)` is a feature — the handbook shows the
--     spread across sources/conditions; nobody picks a canonical number at
--     write time. `material_ref_id` has no per-kind FK (refs has no
--     per-kind check) — the handler enforces `kind='material'` at write
--     time. `input_unit` is reserved, always NULL in v1 (the
--     unit-conversion follow-on's write column).
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

-- 1. the ref kind ------------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('material', FALSE, 'Material',
     'CRC-handbook-style engineering material properties store — a slug '
     'entity (name/aliases/class) plus per-property sourced values in a '
     'typed, growable property registry. v1 is canonical-units-only: a '
     'unit that is not the property''s canonical unit is rejected, named. '
     'See precis-material-help.')
ON CONFLICT (slug) DO NOTHING;

-- 2. the property registry ---------------------------------------------
CREATE TABLE IF NOT EXISTS material_properties (
    prop_id          text PRIMARY KEY,
    name             text NOT NULL,
    canonical_unit   text,               -- NULL = dimensionless/categorical
    dimension        text,               -- quantity-kind label (descriptive, v1)
    value_type       text NOT NULL
        CHECK (value_type IN ('quantity', 'ratio', 'categorical', 'boolean', 'text')),
    allowed_values   jsonb,              -- closed set for categoricals; else NULL
    standard_ref     text,               -- ASTM/ISO/IUPAC/QUDT URI for the property itself
    status           text NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('core', 'proposed')),
    higher_is_better boolean,            -- reserved for later selection use
    description      text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE material_properties IS
    'Typed, growable material-property registry (materials-handbook-kind '
    'proposal). core = curated starter set; proposed = minted at write '
    'time (must declare a canonical unit + dimension), never silently '
    'promoted.';

-- 3. the value fact table -----------------------------------------------
CREATE TABLE IF NOT EXISTS material_values (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    material_ref_id  bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    property_id      text NOT NULL REFERENCES material_properties (prop_id),
    value_num        double precision,   -- in the property's canonical unit
    value_low        double precision,   -- range / uncertainty lower bound
    value_high       double precision,   -- range / uncertainty upper bound
    value_text       text,               -- categorical / text value
    value_bool       boolean,
    input_unit       text,               -- reserved for the unit-conversion
                                          -- follow-on; always NULL in v1
    conditions       jsonb NOT NULL DEFAULT '{}'::jsonb,
    maturity         text NOT NULL DEFAULT 'lab'
        CHECK (maturity IN ('commercial', 'lab', 'speculative')),
    method           text,               -- measured | datasheet | dft | estimated | ...
    source_ref_id    bigint REFERENCES refs (ref_id) ON DELETE SET NULL,
    source_chunk     text,               -- chunk handle into source_ref_id
    source_url       text,               -- fallback source, no ref
    as_of            date,               -- load-bearing for cost_per_mass etc.
    set_by           text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    notes            text
);

COMMENT ON TABLE material_values IS
    'materials-handbook-kind proposal: one sourced measurement per row. '
    'material_ref_id is handler-enforced to kind=material (refs has no '
    'per-kind FK). value_num is stored in the property''s canonical unit '
    '(v1 does no conversion).';

CREATE INDEX IF NOT EXISTS material_values_prop_value_idx
    ON material_values (property_id, value_num);
CREATE INDEX IF NOT EXISTS material_values_material_idx
    ON material_values (material_ref_id);

-- 4. core seed set (temperatures in Kelvin — absolute scale) -----------
INSERT INTO material_properties
    (prop_id, name, canonical_unit, dimension, value_type, status, description)
VALUES
    ('density', 'Density', 'kg/m3', 'mass/volume', 'quantity', 'core',
     'Mass per unit volume.'),
    ('tensile_strength_yield', 'Tensile yield strength', 'MPa', 'pressure/stress',
     'quantity', 'core', 'Stress at the onset of plastic deformation (0.2% offset).'),
    ('tensile_strength_ultimate', 'Ultimate tensile strength', 'MPa', 'pressure/stress',
     'quantity', 'core', 'Maximum engineering stress before necking/fracture.'),
    ('youngs_modulus', 'Young''s modulus', 'GPa', 'pressure/stress', 'quantity', 'core',
     'Elastic (tensile/compressive) stiffness.'),
    ('shear_modulus', 'Shear modulus', 'GPa', 'pressure/stress', 'quantity', 'core',
     'Elastic shear stiffness.'),
    ('poissons_ratio', 'Poisson''s ratio', NULL, 'dimensionless', 'ratio', 'core',
     'Negative ratio of transverse to axial strain.'),
    ('elongation_at_break', 'Elongation at break', '%', 'dimensionless', 'ratio', 'core',
     'Engineering strain at fracture in a tensile test.'),
    ('hardness_vickers', 'Vickers hardness', 'HV', 'hardness (non-convertible scale)',
     'quantity', 'core', 'Indentation hardness on the Vickers scale.'),
    ('thermal_conductivity', 'Thermal conductivity', 'W/(m*K)',
     'power/(length*temperature)', 'quantity', 'core',
     'Rate of heat transfer through a unit thickness per unit temperature gradient.'),
    ('specific_heat_capacity', 'Specific heat capacity', 'J/(kg*K)',
     'energy/(mass*temperature)', 'quantity', 'core',
     'Heat required to raise unit mass by one kelvin.'),
    ('thermal_expansion_coeff', 'Coefficient of thermal expansion', '1/K',
     '1/temperature', 'quantity', 'core',
     'Fractional length change per kelvin (linear CTE).'),
    ('melting_point', 'Melting point', 'K', 'temperature', 'quantity', 'core',
     'Solid-to-liquid transition temperature. Absolute scale (Kelvin).'),
    ('max_service_temperature', 'Maximum service temperature', 'K', 'temperature',
     'quantity', 'core',
     'Upper continuous-use temperature before properties degrade. Absolute scale.'),
    ('electrical_resistivity', 'Electrical resistivity', 'ohm*m', 'resistance*length',
     'quantity', 'core', 'Bulk resistivity to electrical current.'),
    ('dielectric_strength', 'Dielectric strength', 'MV/m', 'voltage/length', 'quantity',
     'core', 'Maximum electric field before insulation breakdown.'),
    ('relative_permittivity', 'Relative permittivity', NULL, 'dimensionless', 'ratio',
     'core', 'Permittivity relative to vacuum (dielectric constant).'),
    ('cost_per_mass', 'Cost per unit mass', 'USD/kg', 'currency/mass', 'quantity',
     'core', 'Market cost per unit mass; pair with as_of (load-bearing for cost).')
ON CONFLICT (prop_id) DO NOTHING;

-- 5. proposed-tier examples (exercise the tiering; never silently core) --
INSERT INTO material_properties
    (prop_id, name, canonical_unit, dimension, value_type, allowed_values, status,
     description)
VALUES
    ('crystal_structure', 'Crystal structure', NULL, 'categorical', 'categorical',
     '["FCC", "BCC", "HCP"]'::jsonb, 'proposed',
     'Crystallographic lattice type.'),
    ('is_magnetic', 'Is magnetic', NULL, 'categorical', 'boolean', NULL, 'proposed',
     'Whether the material is ferro/ferrimagnetic at room temperature.')
ON CONFLICT (prop_id) DO NOTHING;

COMMIT;

-- End of 0092_material_kind.sql
