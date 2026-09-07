-- 0157_rxn_kind.sql
--
-- The `rxn` kind — a sourced reaction-fact store (design-of-record
-- docs/backlog/reaction-kind-and-synthesis-cost.md). Deliberately shaped as
-- `material` (mig 0092) with a transformation as the entity instead of a
-- substance, because the load-bearing property is the same one: MANY ROWS PER
-- (entity, property) IS THE FEATURE. Twelve reported yields for one
-- transformation across twelve papers IS the answer; nobody picks a canonical
-- number at write time.
--
--   * the entity is a slug-addressed `refs` row (kind='rxn'); `meta` carries
--     `rxn_smiles`, the two identity keys, and `reaction_class`;
--   * `rxn_properties` — typed growable registry, core/proposed tiers, exactly
--     `material_properties`;
--   * `rxn_values` — the fact table, `material_values` verbatim plus nothing.
--
-- NAMING: the kind is `rxn`, NOT `reaction`. "reaction" is already a value of
-- a field literally named `kind` in the pathway graph edge model
-- (precis_web/routes/refs.py, precis_pathway/toon_views.py emit
-- `"kind": ... or "reaction"`), so an agent reading a pathway TOON table would
-- otherwise call get(kind='reaction', ...) and get a real, wrong answer. Two
-- skills already write "reaction-*network*" in italics to keep `pathway` and
-- `route` apart; `rxn` refuses to make that three-way.
--
-- TWO IDENTITY AXES, both on meta, both indexed:
--   * `uid_strict`   — canonical over the WHOLE balanced equation. Exact
--     identity; dedup; collapses re-imports of the same record.
--   * `uid_transform` — canonical over (reactants, desired product) ONLY,
--     ignoring byproducts. This is the one every query wants: the literature
--     records byproducts inconsistently (A+B>>C and A+B>>C+NaBr are the same
--     chemistry), so a strict-only key fragments precedent across sources.
--   Neither is computed here — the handler owns it, because the canonicaliser
--   is a Python dep (linchemin) that must not reach the request path.
--
-- `reaction_class` stores an RXNO id (CC BY 4.0, 686 classes). RXNO is a
-- DICTIONARY, not a reader — it ships no SMARTS and no SMILES->class mapping;
-- recognition comes from a classifier (Rxn-INSIGHT / reactionclassifier) or
-- from a source dataset's own label. Storing RXNO ids regardless of which
-- classifier ran keeps us interoperable with NameRxn-derived data forever.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

-- 1. the ref kind ------------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('rxn', FALSE, 'Reaction',
     'A sourced reaction-fact store: a transformation (reaction SMILES) plus '
     'per-property sourced values (yield, temperature, time, catalyst '
     'loading, ...) in a typed, growable registry. Many rows per '
     '(reaction, property) is the point — the spread across sources and '
     'conditions IS the answer. Named `rxn` not `reaction` to avoid '
     'colliding with the pathway graph''s `reaction` edge kind. '
     'See precis-rxn-help.')
ON CONFLICT (slug) DO NOTHING;

-- 2. the property registry ---------------------------------------------
CREATE TABLE IF NOT EXISTS rxn_properties (
    prop_id          text PRIMARY KEY,
    name             text NOT NULL,
    canonical_unit   text,               -- NULL = dimensionless/categorical
    dimension        text,               -- quantity-kind label (descriptive)
    value_type       text NOT NULL
        CHECK (value_type IN ('quantity', 'ratio', 'categorical', 'boolean', 'text')),
    allowed_values   jsonb,              -- closed set for categoricals; else NULL
    standard_ref     text,               -- IUPAC/ontology URI for the property
    status           text NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('core', 'proposed')),
    higher_is_better boolean,
    description      text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE rxn_properties IS
    'Typed, growable reaction-property registry (mirrors '
    'material_properties). core = curated starter set; proposed = minted at '
    'write time (must declare a canonical unit + dimension), never silently '
    'promoted.';

-- 3. the value fact table -----------------------------------------------
CREATE TABLE IF NOT EXISTS rxn_values (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rxn_ref_id       bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    property_id      text NOT NULL REFERENCES rxn_properties (prop_id),
    value_num        double precision,   -- in the property's canonical unit
    value_low        double precision,   -- range / uncertainty lower bound
    value_high       double precision,   -- range / uncertainty upper bound
    value_text       text,               -- categorical / text value
    value_bool       boolean,
    input_unit       text,               -- reserved (unit-conversion follow-on)
    conditions       jsonb NOT NULL DEFAULT '{}'::jsonb,
    maturity         text NOT NULL DEFAULT 'lab'
        CHECK (maturity IN ('commercial', 'lab', 'speculative')),
    -- HOW the value was obtained. Unlike material/component (where the column
    -- exists but no verb can reach it — gripe 329157), `method` is wired
    -- end-to-end here: it is the axis separating a curated measured yield from
    -- a bulk patent-extracted or predicted one, and the "N of M grounded"
    -- coverage line is meaningless without it.
    method           text,
    -- Licence of the SOURCE this row came from. Load-bearing for price and for
    -- anything published: an NC- or non-redistributable-licensed number must
    -- never leak into a nanopub. NULL = unrecorded, treat as unpublishable.
    source_licence   text,
    source_ref_id    bigint REFERENCES refs (ref_id) ON DELETE SET NULL,
    source_chunk     text,               -- chunk handle into source_ref_id
    source_url       text,               -- fallback source, no ref
    as_of            date,               -- load-bearing for price
    set_by           text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    notes            text
);

COMMENT ON TABLE rxn_values IS
    'One sourced reaction measurement per row. rxn_ref_id is handler-enforced '
    'to kind=rxn (refs has no per-kind check). value_num is stored in the '
    'property''s canonical unit (v1 does no conversion). Multiple rows per '
    '(rxn, property) is intended — the spread is the finding.';

CREATE INDEX IF NOT EXISTS rxn_values_prop_value_idx
    ON rxn_values (property_id, value_num);
CREATE INDEX IF NOT EXISTS rxn_values_rxn_idx
    ON rxn_values (rxn_ref_id);
CREATE INDEX IF NOT EXISTS rxn_values_source_ref_idx
    ON rxn_values (source_ref_id);

-- 4. identity-key indexes on the entity meta ----------------------------
-- The transform key is the one precedent queries hit; the strict key is the
-- dedup/collapse key for imports. Partial so the index only covers rows that
-- actually carry a key.
CREATE INDEX IF NOT EXISTS refs_rxn_uid_transform_idx
    ON refs ((meta ->> 'uid_transform'))
    WHERE kind = 'rxn' AND meta ? 'uid_transform';
CREATE INDEX IF NOT EXISTS refs_rxn_uid_strict_idx
    ON refs ((meta ->> 'uid_strict'))
    WHERE kind = 'rxn' AND meta ? 'uid_strict';
CREATE INDEX IF NOT EXISTS refs_rxn_class_idx
    ON refs ((meta ->> 'reaction_class'))
    WHERE kind = 'rxn' AND meta ? 'reaction_class';

-- 5. core seed set -------------------------------------------------------
-- Temperatures in Kelvin (absolute scale, matching material). Time in seconds.
-- `yield` is ONE property: isolated-vs-assay is a `conditions` key, matching
-- how material handles measurement method.
INSERT INTO rxn_properties
    (prop_id, name, canonical_unit, dimension, value_type, status, higher_is_better,
     description)
VALUES
    ('yield', 'Yield', '%', 'dimensionless', 'ratio', 'core', TRUE,
     'Fraction of theoretical product obtained. Isolated vs assay/NMR yield is '
     'a conditions key, not a separate property.'),
    ('temperature', 'Temperature', 'K', 'temperature', 'quantity', 'core', NULL,
     'Reaction temperature. Absolute scale (Kelvin).'),
    ('time', 'Reaction time', 's', 'time', 'quantity', 'core', FALSE,
     'Elapsed reaction time.'),
    ('pressure', 'Pressure', 'Pa', 'pressure', 'quantity', 'core', NULL,
     'Reaction pressure.'),
    ('catalyst_loading', 'Catalyst loading', 'mol%', 'dimensionless', 'ratio',
     'core', FALSE, 'Catalyst charge relative to limiting reagent.'),
    ('scale', 'Scale', 'mol', 'amount', 'quantity', 'core', NULL,
     'Amount of limiting reagent. A yield at 1 mmol and at 1 mol are different '
     'claims.'),
    ('ee', 'Enantiomeric excess', '%', 'dimensionless', 'ratio', 'core', TRUE,
     'Enantiomeric excess of the product.'),
    ('atom_economy', 'Atom economy', '%', 'dimensionless', 'ratio', 'core', TRUE,
     'Mass of desired product over summed mass of reactants. Computable from '
     'stoichiometry alone.'),
    ('solvent', 'Solvent', NULL, 'dimensionless', 'text', 'core', NULL,
     'Reaction solvent. Text in v1; a solvent entity is a later refinement.'),
    ('price_per_gram', 'Price per gram', 'USD/g', 'currency/mass', 'quantity',
     'core', FALSE,
     'Purchase price of a buyable compound. A price is a property of '
     '(compound, vendor, pack size, date) — vendor and pack size belong in '
     'conditions, the date in as_of, and the source licence in source_licence.')
ON CONFLICT (prop_id) DO NOTHING;

COMMIT;
