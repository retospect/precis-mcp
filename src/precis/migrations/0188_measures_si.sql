-- 0188_measures_si.sql
--
-- Measures Build A2 (docs/backlog/measures-substrate.md, "Store SI, convert at
-- the edges" and "Build A2"; Reto, 2026-10-03). Three things, one transaction:
--
--   * The legacy taxa seeded by 0174 move to coherent SI. A new table,
--     `measure_unit_compat`, records for each such taxon the unit it was kept
--     in (legacy_unit), the SI unit it now stores (si_unit) and the linear map
--     `si = legacy * factor + si_offset`. It is keyed by the taxon's STABLE
--     identifier, `(legacy_table, legacy_key)` = the taxon's
--     `meta.legacy_source`, never by ref_id (ref ids differ across installs).
--     Each converted taxon's `canonical_unit` becomes the SI unit and its
--     `display_unit` the legacy unit, so every reader that converts at the edge
--     (the Build B reads) shows the number it always did.
--
--   * Every live numeric legacy row L on such a taxon is superseded by a new
--     row N holding the SI value (rows are append-only; `measures_frozen`
--     allows exactly this one supersession). N copies L except: value_num,
--     value_low, value_high, value_err (mapped), `actor` ('migration-0188'),
--     `supersedes` (L.id) and `meta` (L.meta plus `rebased_from`,
--     `legacy_actor`, `conversion`). NULL stays NULL. `created_at`, `literal`,
--     `reported_unit`, `run_key` and the anchors are kept. L stays readable,
--     with its chain, through get(kind='measure', id=L); its numbers are in the
--     legacy unit, which N's meta.conversion records.
--
--   * `rxn_values` is dropped, with no compatibility view: reaction values are
--     plain `measures` rows (subject = the rxn ref, measurand = the
--     `rxn_properties` taxon) and `store/_rxn_ops.py` is ported in the same
--     branch. The migration REFUSES to run when the table holds a row.
--
-- The compatibility views (material_values, component_spec_values) read back
-- through `measure_unit_compat` (legacy = (si - si_offset) / factor), and their
-- INSTEAD OF INSERT trigger converts the legacy writer's number the other way,
-- so `store/_material_ops.py` and `store/_component_ops.py` run unchanged.
-- `precis_measure_taxon` mints a taxon for a legacy property that has none
-- already in SI (canonical = si_unit, display_unit = legacy_unit) when a compat
-- row exists, so a database whose taxa are minted lazily (a baseline-built
-- install, where the 0174 taxa are not seed rows) gets the same numbers.
--
-- The unit guard (0187) refuses a canonical_unit change while live measures
-- exist. This migration lifts it with the transaction-local
-- `precis.allow_unit_rebase = 'on'`, which the guard honours only when the OLD
-- canonical_unit equals the compat row's legacy_unit AND the NEW one equals its
-- si_unit (and nothing else about the taxon changes): a second re-base, or any
-- other change, is refused even with the setting on.
--
-- Scope: 15 mm taxa (including width) and 30 compat rows in all (USD/g was
-- dropped: no conversion for currency).
--
-- Legacy properties minted AFTER this migration: the legacy mint verbs
-- (material_property_mint, component_spec_mint, rxn_property_mint) convert at the
-- edge. A unit with an SI form (nm, MPa, %, degC, ...) mints as before, and the
-- same transaction inserts a measure_unit_compat row for the new key (the factor
-- from a seeded row with that legacy_unit, else from pint, as exact decimals), so
-- its taxon stores SI and the views convert exactly as for the seeded rows. A unit
-- that is already coherent SI, or has no SI form (USD, HV, pH), writes no row and
-- the taxon keeps that unit.
--
-- Every lock wait is bounded: SET LOCAL lock_timeout = '5s' is the first statement
-- after BEGIN (transaction-local, never a session SET), so a deploy that meets a
-- long transaction fails cleanly and is retried instead of queueing behind it.
--
-- Statement order: the rxn_values lock and emptiness refusal first; then new
-- objects, a SHARE ROW EXCLUSIVE lock on measures (blocks measure writers only,
-- closing the window between the scans' snapshot and COMMIT) and every scan and
-- backfill, with the formula half of the round-trip check right after the
-- backfill; then the statements that lock hot tables (UPDATE kinds, the taxon
-- UPDATE on refs, the view replacement, DROP TABLE rxn_values, which references
-- refs) last; the view half of the round-trip check, which needs the final state
-- and reads only, then DROP TABLE rxn_values (so its lock is held for the drop
-- only) end the file.
--
-- Window between this migration and the service restart: OLD code could store a
-- UNITLESS numeric on a converted taxon as if it were SI (old insert_measure
-- accepts it). The BEFORE INSERT trigger measures_unitless_guard (created here,
-- kept for good) refuses it. Unit-bearing writes are converted correctly, because
-- old code reads the taxon's new canonical_unit. The deploy restarts the serve
-- processes right after migrate.
-- Forward-only (ADR 0005).

BEGIN;

-- Bounded lock waits for the whole transaction (see the header).
SET LOCAL lock_timeout = '5s';

-- ── 1. refuse to drop rxn_values unless it is empty ────────────────────

-- Locked first, so no row can slip in between the check and the DROP below.
LOCK TABLE rxn_values IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM rxn_values) THEN
        RAISE EXCEPTION
            'rxn_values holds % rows; 0188 drops it and must not run '
            '(move them into measures first)', (SELECT count(*) FROM rxn_values);
    END IF;
END $$;

-- ── 2. the per-taxon conversion table ──────────────────────────────────

CREATE TABLE measure_unit_compat (
    legacy_table text NOT NULL
        CHECK (legacy_table IN ('material_properties', 'component_specs',
                                'rxn_properties')),
    legacy_key   text NOT NULL,
    legacy_unit  text NOT NULL,
    si_unit      text NOT NULL,
    factor       numeric NOT NULL CHECK (factor > 0),
    si_offset    numeric NOT NULL DEFAULT 0,
    PRIMARY KEY (legacy_table, legacy_key),
    CHECK (legacy_unit <> si_unit)
);

COMMENT ON TABLE measure_unit_compat IS
    'Units the legacy taxa (meta.legacy_source = {table, key}, seeded by 0174) were kept in, and the SI unit they store now: si = legacy * factor + si_offset, computed in numeric (factor and offset are exact decimals) and cast to float8, so 25.4 mm is exactly 0.0254 m. Keyed by the taxon''s stable identifier, not its ref_id. Read by the material_values / component_spec_values views and their insert triggers, by precis_measure_taxon (a minted legacy taxon starts in SI) and by the store''s legacy-unit writers. Seed vocabulary: it rides in the baseline.';
COMMENT ON COLUMN measure_unit_compat.si_offset IS
    'Additive part of the map (0 for every unit today; reserved for the first affine legacy unit). An uncertainty scales by factor only.';

-- Read-only prod inventory, 2026-10-04 (77 legacy taxa): the units below are the
-- only legacy canonical units that are not already coherent SI or a unit with no
-- SI form (HV, USD, USD/g, USD/kg) or categorical. 0 offsets: no affine legacy
-- unit. price_per_gram keeps USD/g with no conversion: currency is outside SI,
-- pint cannot convert it, and it has 0 rows.
INSERT INTO measure_unit_compat
    (legacy_table, legacy_key, legacy_unit, si_unit, factor) VALUES
    ('component_specs', 'across_flats',          'mm',   'm',     1e-3),
    ('component_specs', 'bore_diameter',         'mm',   'm',     1e-3),
    ('component_specs', 'bore_diameter_bearing', 'mm',   'm',     1e-3),
    ('component_specs', 'drive_size',            'mm',   'm',     1e-3),
    ('component_specs', 'head_diameter',         'mm',   'm',     1e-3),
    ('component_specs', 'head_height',           'mm',   'm',     1e-3),
    ('component_specs', 'height',                'mm',   'm',     1e-3),
    ('component_specs', 'inner_diameter',        'mm',   'm',     1e-3),
    ('component_specs', 'length',                'mm',   'm',     1e-3),
    ('component_specs', 'min_bend_radius',       'mm',   'm',     1e-3),
    ('component_specs', 'outer_diameter',        'mm',   'm',     1e-3),
    ('component_specs', 'thickness',             'mm',   'm',     1e-3),
    ('component_specs', 'thread_pitch',          'mm',   'm',     1e-3),
    ('component_specs', 'wall_thickness',        'mm',   'm',     1e-3),
    ('component_specs', 'width',                 'mm',   'm',     1e-3),
    ('component_specs', 'max_working_pressure',  'MPa',  'Pa',    1e6),
    ('component_specs', 'head_angle',            'deg',  'rad',   0.01745329251994329577),
    ('material_properties', 'persistence_length',        'nm',   'm',    1e-9),
    ('material_properties', 'unit_length',               'nm',   'm',    1e-9),
    ('material_properties', 'delta_length',              'Å',    'm',    1e-10),
    ('material_properties', 'tensile_strength_ultimate', 'MPa',  'Pa',   1e6),
    ('material_properties', 'tensile_strength_yield',    'MPa',  'Pa',   1e6),
    ('material_properties', 'shear_modulus',             'GPa',  'Pa',   1e9),
    ('material_properties', 'youngs_modulus',            'GPa',  'Pa',   1e9),
    ('material_properties', 'dielectric_strength',       'MV/m', 'V/m',  1e6),
    ('material_properties', 'elongation_at_break',       '%',    '1',    1e-2),
    ('rxn_properties', 'atom_economy',     '%',     '1',      1e-2),
    ('rxn_properties', 'ee',               '%',     '1',      1e-2),
    ('rxn_properties', 'yield',            '%',     '1',      1e-2),
    ('rxn_properties', 'catalyst_loading', 'mol%',  '1',      1e-2)
ON CONFLICT (legacy_table, legacy_key) DO NOTHING;

-- ── 3. functions (catalog only; no table locks) ────────────────────────

-- precis_measure_taxon (0187) with two additions: the rxn_properties registry
-- (rxn_values is gone, so a reaction property proposed after 0174 needs a
-- taxon too) and the compat table (a minted legacy taxon starts in SI).
CREATE OR REPLACE FUNCTION precis_measure_taxon(
    p_table text, p_key text, p_create boolean DEFAULT FALSE)
    RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    v_src   jsonb := jsonb_build_object('table', p_table, 'key', p_key);
    v_id    bigint;
    v_name  text;
    v_unit  text;
    v_disp  text;
    v_vtype text;
    v_allow jsonb;
    v_std   text;
    v_hib   boolean;
    v_desc  text;
    v_def   text;
    v_dkind text;
    v_alias jsonb;
    v_meta  jsonb;
    v_start bigint;
    v_si    text;
BEGIN
    SELECT r.ref_id INTO v_id
      FROM refs r
     WHERE r.kind = 'taxon'
       AND r.meta -> 'legacy_source' ->> 'table' = p_table
       AND r.meta -> 'legacy_source' ->> 'key' = p_key
     ORDER BY (r.retired_at IS NOT NULL), r.ref_id
     LIMIT 1;
    IF v_id IS NOT NULL OR NOT p_create THEN
        RETURN v_id;
    END IF;

    -- one minter per (table, key); the loser of a race waits here, then
    -- re-checks and finds the winner's taxon
    PERFORM pg_advisory_xact_lock(
        hashtext('precis_measure_taxon:' || p_table || ':' || p_key));
    SELECT r.ref_id INTO v_id
      FROM refs r
     WHERE r.kind = 'taxon'
       AND r.meta -> 'legacy_source' ->> 'table' = p_table
       AND r.meta -> 'legacy_source' ->> 'key' = p_key
     ORDER BY (r.retired_at IS NOT NULL), r.ref_id
     LIMIT 1;
    IF v_id IS NOT NULL THEN
        RETURN v_id;
    END IF;

    IF p_table = 'material_properties' THEN
        SELECT name, canonical_unit, value_type, allowed_values, standard_ref,
               higher_is_better, description
          INTO v_name, v_unit, v_vtype, v_allow, v_std, v_hib, v_desc
          FROM material_properties WHERE prop_id = p_key;
    ELSIF p_table = 'component_specs' THEN
        SELECT name, canonical_unit, value_type, allowed_values, standard_ref,
               higher_is_better, description
          INTO v_name, v_unit, v_vtype, v_allow, v_std, v_hib, v_desc
          FROM component_specs WHERE spec_id = p_key;
    ELSIF p_table = 'rxn_properties' THEN
        SELECT name, canonical_unit, value_type, allowed_values, standard_ref,
               higher_is_better, description
          INTO v_name, v_unit, v_vtype, v_allow, v_std, v_hib, v_desc
          FROM rxn_properties WHERE prop_id = p_key;
    ELSE
        RETURN NULL;
    END IF;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    v_name := regexp_replace(btrim(v_name), '\s+', ' ', 'g');
    v_def := btrim(coalesce(v_desc, ''));
    IF v_def = '' THEN
        v_def := format(
            '%s: a %s term%s, carried over from the legacy %s registry, which recorded no fuller definition.',
            v_name, v_vtype,
            CASE WHEN v_unit IS NOT NULL THEN ' measured in ' || v_unit ELSE '' END,
            p_table);
    END IF;
    -- the legacy registry keeps its own unit; the taxon stores SI
    SELECT c.si_unit INTO v_si FROM measure_unit_compat c
     WHERE c.legacy_table = p_table AND c.legacy_key = p_key
       AND c.legacy_unit = v_unit;
    IF v_si IS NOT NULL THEN
        v_disp := v_unit;
        v_unit := v_si;
    END IF;
    v_dkind := CASE
        WHEN v_unit IS NOT NULL THEN NULL      -- unmapped; see taxon /unmapped
        WHEN v_vtype IN ('categorical', 'boolean', 'text') THEN 'categorical'
        ELSE 'dimensionless' END;
    v_alias := CASE WHEN lower(v_name) <> lower(p_key)
                    THEN jsonb_build_array(p_key) ELSE '[]'::jsonb END;
    v_meta := jsonb_strip_nulls(jsonb_build_object(
        'name', v_name,
        'norm_name', lower(v_name),
        'slug', trim(both '-' from regexp_replace(lower(v_name), '[^a-z0-9]+', '-', 'g')),
        'definition', v_def,
        'aliases', v_alias,
        'status', 'proposed',
        'dimension_kind', v_dkind,
        'canonical_unit', v_unit,
        'display_unit', v_disp,
        'value_type', v_vtype,
        'allowed_values', v_allow,
        'standard_ref', v_std,
        'higher_is_better', v_hib,
        'legacy_source', v_src));

    INSERT INTO refs (kind, title, meta) VALUES ('taxon', v_name, v_meta)
    RETURNING ref_id INTO v_id;
    RAISE NOTICE 'precis_measure_taxon: minted taxon % for %.%', v_id, p_table, p_key;

    INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta)
    VALUES (v_id, -1, 'card_combined',
            v_name || ' — ' || v_def
                || CASE WHEN jsonb_array_length(v_alias) > 0
                        THEN ' (aka ' || p_key || ')' ELSE '' END,
            '{}'::jsonb);

    SELECT r.ref_id INTO v_start
      FROM refs r
     WHERE r.kind = 'taxon' AND r.meta ->> 'start' = 'true'
       AND r.meta ->> 'slug' = 'measurand'
     ORDER BY r.ref_id LIMIT 1;
    IF v_start IS NOT NULL THEN
        INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by)
        VALUES (v_id, v_start, 'specialises', 'system')
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN v_id;
END
$$;

-- SI back to the legacy unit: (si - offset) / factor in numeric, from the float's
-- shortest round-trip text (not the 15-digit float8 -> numeric cast, which loses
-- the last digits of 90 deg = 1.5707963267948966 rad), then rounded to 15
-- significant digits, so a value written as 7 reads back as 7, not
-- 7.000000000000001. The round trip is checked to 1e-12 regardless.
CREATE OR REPLACE FUNCTION precis_measure_legacy_value(
    p_si double precision, p_factor numeric, p_offset numeric)
    RETURNS double precision LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_si IS NULL OR p_factor IS NULL THEN p_si
        WHEN p_si IN ('Infinity'::float8, '-Infinity'::float8) THEN p_si
        ELSE ((((p_si::text::numeric - coalesce(p_offset, 0)) / p_factor)::double precision)
              ::numeric)::double precision
    END
$$;

-- Legacy unit to SI: legacy * factor + offset in numeric (exact decimals), then
-- cast to float8, so 25.4 mm is exactly 0.0254 m and not 0.025400000000000002 (the
-- float8 product). The migration, the view's insert trigger and the store's
-- insert_measure (Decimal) all compute it this way, so they store the same bits.
-- No factor: the number passes through untouched.
CREATE OR REPLACE FUNCTION precis_measure_si_value(
    p_legacy double precision, p_factor numeric, p_offset numeric)
    RETURNS double precision LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_legacy IS NULL OR p_factor IS NULL THEN p_legacy
        WHEN p_legacy IN ('Infinity'::float8, '-Infinity'::float8) THEN p_legacy
        ELSE ((p_legacy::numeric * p_factor + coalesce(p_offset, 0))::double precision)
    END
$$;

-- The unit guard (0187) with the one-way, one-time bypass: a re-base is accepted
-- only inside a transaction that set precis.allow_unit_rebase = 'on', only when
-- the taxon's OLD canonical_unit is the compat row's legacy_unit and the NEW one
-- its si_unit, and only when nothing else the guard watches changes. After the
-- re-base the old unit is the SI one, so no later transaction can repeat it.
CREATE OR REPLACE FUNCTION precis_taxon_unit_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_canon boolean :=
        OLD.meta -> 'canonical_unit' IS DISTINCT FROM NEW.meta -> 'canonical_unit';
    v_dim boolean :=
        (OLD.meta -> 'dimension_kind' IS DISTINCT FROM NEW.meta -> 'dimension_kind'
         AND coalesce(OLD.meta -> 'dimension_kind', 'null'::jsonb) <> 'null'::jsonb)
        OR (OLD.meta -> 'si_vector' IS DISTINCT FROM NEW.meta -> 'si_vector'
            AND coalesce(OLD.meta -> 'si_vector', 'null'::jsonb) <> 'null'::jsonb);
BEGIN
    IF (v_canon OR v_dim) AND EXISTS (SELECT 1 FROM measures
                WHERE measurand_ref_id = OLD.ref_id AND superseded_by IS NULL) THEN
        IF v_canon AND NOT v_dim
           AND coalesce(current_setting('precis.allow_unit_rebase', true), '') = 'on'
           AND OLD.meta -> 'legacy_source' IS NOT DISTINCT FROM NEW.meta -> 'legacy_source'
           AND EXISTS (SELECT 1 FROM measure_unit_compat c
                        WHERE c.legacy_table = OLD.meta -> 'legacy_source' ->> 'table'
                          AND c.legacy_key = OLD.meta -> 'legacy_source' ->> 'key'
                          AND c.legacy_unit = OLD.meta ->> 'canonical_unit'
                          AND c.si_unit = NEW.meta ->> 'canonical_unit') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'taxon % has live measures stored in canonical_unit % (dimension_kind %, '
            'si_vector %); changing canonical_unit, or changing dimension_kind / '
            'si_vector from a value, would silently re-base them (supersede the '
            'measures first)',
            OLD.ref_id, OLD.meta ->> 'canonical_unit', OLD.meta ->> 'dimension_kind',
            OLD.meta ->> 'si_vector'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

-- The compatibility views' insert trigger (0187) with the unit conversion: the
-- legacy writer's number is in the legacy unit; the taxon stores SI. A taxon
-- with no compat row is written as before. A compat row whose taxon is not in
-- the SI unit (a re-base that did not happen) refuses rather than store a
-- number a thousand times off.
CREATE OR REPLACE FUNCTION precis_legacy_value_insert()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_table  text := TG_ARGV[0];
    v_key    text;
    v_tax    bigint;
    v_id     bigint;
    v_subj   bigint;
    c        measure_unit_compat;
    v_canon  text;
    v_meta   jsonb := '{}'::jsonb;
    v_runit  text;
    f        numeric;
    o        numeric;
BEGIN
    IF v_table = 'material_properties' THEN
        v_key := to_jsonb(NEW) ->> 'property_id';
        v_subj := (to_jsonb(NEW) ->> 'material_ref_id')::bigint;
    ELSE
        v_key := to_jsonb(NEW) ->> 'spec_id';
        v_subj := (to_jsonb(NEW) ->> 'component_ref_id')::bigint;
    END IF;
    v_tax := precis_measure_taxon(v_table, v_key, TRUE);
    IF v_tax IS NULL THEN
        RAISE EXCEPTION '% % is not registered', v_table, v_key
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    SELECT * INTO c FROM measure_unit_compat
     WHERE legacy_table = v_table AND legacy_key = v_key;
    IF FOUND THEN
        SELECT meta ->> 'canonical_unit' INTO v_canon FROM refs WHERE ref_id = v_tax;
        IF v_canon IS DISTINCT FROM c.si_unit THEN
            RAISE EXCEPTION
                '% % is stored in % but its compat row converts to %: refusing to '
                'write a legacy number that could be off by %',
                v_table, v_key, v_canon, c.si_unit, c.factor
                USING ERRCODE = 'check_violation';
        END IF;
        f := c.factor;
        o := c.si_offset;
        v_runit := c.legacy_unit;
        v_meta := jsonb_build_object('conversion', jsonb_build_object(
                      'from', c.legacy_unit, 'to', c.si_unit));
    END IF;
    v_id := nextval(pg_get_serial_sequence('measures', 'id'));
    INSERT INTO measures
        (id, subject_ref_id, measurand_ref_id, value_num, value_low, value_high,
         value_text, value_bool, input_unit, conditions, maturity, method,
         source_ref_id, source_chunk, source_url, as_of, actor, notes,
         literal, value_form, run_key, reported_unit, meta)
    OVERRIDING SYSTEM VALUE
    VALUES
        (v_id, v_subj, v_tax, precis_measure_si_value(NEW.value_num, f, o),
         precis_measure_si_value(NEW.value_low, f, o),
         precis_measure_si_value(NEW.value_high, f, o),
         NEW.value_text, NEW.value_bool, NEW.input_unit,
         coalesce(NEW.conditions, '{}'::jsonb), coalesce(NEW.maturity, 'lab'),
         NEW.method, NEW.source_ref_id, NEW.source_chunk, NEW.source_url,
         NEW.as_of, coalesce(nullif(btrim(NEW.set_by), ''), 'legacy'), NEW.notes,
         precis_measure_literal(NEW.value_num, NEW.value_low, NEW.value_high,
                                NEW.value_text, NEW.value_bool),
         precis_measure_form(NEW.value_num, NEW.value_low, NEW.value_high,
                             NEW.value_text, NEW.value_bool),
         'legacy:' || v_id, v_runit, v_meta);
    NEW.id := v_id;
    NEW.created_at := now();
    RETURN NEW;
END
$$;

-- ── 4. scans ───────────────────────────────────────────────────────────

-- From here to COMMIT no measure can be written or changed by another session
-- (readers and every other table are unaffected): the scans' snapshot stays true.
LOCK TABLE measures IN SHARE ROW EXCLUSIVE MODE;

-- Every taxon that has a compat row must be in the legacy unit (about to be
-- converted) or already in the SI one. Anything else was edited by hand: the
-- conversion factor would be wrong for it, so stop and say which.
DO $$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(format('%s (%s.%s in %s)', t.ref_id,
                             t.meta -> 'legacy_source' ->> 'table',
                             t.meta -> 'legacy_source' ->> 'key',
                             coalesce(t.meta ->> 'canonical_unit', 'no unit')), ', ')
      INTO bad
      FROM refs t
      JOIN measure_unit_compat c
        ON c.legacy_table = t.meta -> 'legacy_source' ->> 'table'
       AND c.legacy_key = t.meta -> 'legacy_source' ->> 'key'
     WHERE t.kind = 'taxon'
       AND t.meta ->> 'canonical_unit' IS DISTINCT FROM c.legacy_unit
       AND t.meta ->> 'canonical_unit' IS DISTINCT FROM c.si_unit;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'measures SI re-base: taxon(s) % have a canonical_unit that is neither '
            'the legacy unit nor SI in measure_unit_compat; fix the taxon (or the '
            'compat row) and re-run', bad;
    END IF;
END $$;

-- A taxon about to be re-based must not already carry a display_unit of its own
-- that differs from the legacy unit the compat row will write there.
DO $$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(format('%s (%s.%s: display_unit %s, compat legacy_unit %s)',
                             t.ref_id, c.legacy_table, c.legacy_key,
                             t.meta ->> 'display_unit', c.legacy_unit), ', ')
      INTO bad
      FROM refs t
      JOIN measure_unit_compat c
        ON c.legacy_table = t.meta -> 'legacy_source' ->> 'table'
       AND c.legacy_key = t.meta -> 'legacy_source' ->> 'key'
     WHERE t.kind = 'taxon'
       AND t.meta ->> 'canonical_unit' = c.legacy_unit
       AND t.meta ->> 'display_unit' IS NOT NULL
       AND t.meta ->> 'display_unit' IS DISTINCT FROM c.legacy_unit;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'measures SI re-base: taxon(s) % already have a different display_unit; '
            'clear it (or fix the compat row) and re-run', bad;
    END IF;
END $$;

-- History on a taxon about to be re-based cannot be rewritten (the frozen trigger
-- allows one supersession per row, and a superseded row's numbers would stay in
-- the old unit). Prod has none: only the pilot's new taxa carry measures history.
-- At this point no row is superseded by this migration yet.
DO $$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(format('%s (%s.%s: %s superseded row(s))', t.ref_id,
                             c.legacy_table, c.legacy_key, s.n), ', ')
      INTO bad
      FROM (SELECT measurand_ref_id, count(*) AS n FROM measures
             WHERE superseded_by IS NOT NULL GROUP BY measurand_ref_id) s
      JOIN refs t ON t.ref_id = s.measurand_ref_id
      JOIN measure_unit_compat c
        ON c.legacy_table = t.meta -> 'legacy_source' ->> 'table'
       AND c.legacy_key = t.meta -> 'legacy_source' ->> 'key'
     WHERE t.meta ->> 'canonical_unit' = c.legacy_unit;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'measures SI re-base: taxon(s) % already hold superseded measures in '
            'the legacy unit, which a supersession cannot rewrite; resolve them first',
            bad;
    END IF;
END $$;

-- ── 5. the SI rows: one new row per live numeric legacy row ────────────

-- N copies L (created_at included, so ordering and the legacy views' created_at
-- do not move) except the mapped values, reported_unit (L's, or the legacy unit
-- the literal is in when L recorded none), actor, supersedes and meta. NULL bounds
-- and a NULL error stay NULL. Only rows on a taxon still in the legacy unit are
-- converted (a taxon already in SI keeps its rows).
INSERT INTO measures
    (subject_ref_id, measurand_ref_id, literal, reported_unit, value_num,
     value_low, value_high, value_text, value_bool, value_err, value_form,
     reference, tier, trusted, extraction_status, normalization,
     normalization_status, source_attribution, measurand_status, direction, role,
     run_key, subject, subject_group, experiment_ref_id, derived_from,
     primary_link_id, anchor_scheme, span, supersedes, actor, model, meta,
     input_unit, conditions, maturity, method, source_ref_id, source_chunk,
     source_url, as_of, created_at, notes)
SELECT l.subject_ref_id, l.measurand_ref_id, l.literal,
       coalesce(l.reported_unit, c.legacy_unit),  -- the unit the literal is in
       precis_measure_si_value(l.value_num, c.factor, c.si_offset),
       precis_measure_si_value(l.value_low, c.factor, c.si_offset),
       precis_measure_si_value(l.value_high, c.factor, c.si_offset),
       l.value_text, l.value_bool,
       precis_measure_si_value(l.value_err, c.factor, 0),
       l.value_form, l.reference, l.tier, l.trusted, l.extraction_status,
       l.normalization, l.normalization_status, l.source_attribution,
       l.measurand_status, l.direction, l.role, l.run_key, l.subject,
       l.subject_group, l.experiment_ref_id, l.derived_from, l.primary_link_id,
       l.anchor_scheme, l.span, l.id, 'migration-0188', l.model,
       l.meta || jsonb_build_object(
           'rebased_from', l.id,
           'legacy_actor', l.actor,
           'conversion', jsonb_build_object(
               'from', c.legacy_unit, 'to', c.si_unit,
               'factor', c.factor, 'offset', c.si_offset)),
       l.input_unit, l.conditions, l.maturity, l.method, l.source_ref_id,
       l.source_chunk, l.source_url, l.as_of, l.created_at, l.notes
  FROM measures l
  JOIN refs t ON t.ref_id = l.measurand_ref_id
  JOIN measure_unit_compat c
    ON c.legacy_table = t.meta -> 'legacy_source' ->> 'table'
   AND c.legacy_key = t.meta -> 'legacy_source' ->> 'key'
 WHERE l.superseded_by IS NULL
   AND t.meta ->> 'canonical_unit' = c.legacy_unit
   AND (l.value_num IS NOT NULL OR l.value_low IS NOT NULL
        OR l.value_high IS NOT NULL OR l.value_err IS NOT NULL)
 ORDER BY l.id;

-- Close each L onto its N: the frozen trigger's one allowed transition (same
-- subject, label, measurand and direction; N live).
UPDATE measures l
   SET superseded_by = n.id, superseded_at = now()
  FROM measures n
 WHERE n.supersedes = l.id AND n.actor = 'migration-0188'
   AND l.superseded_by IS NULL;

DO $$
DECLARE
    n_left bigint;
BEGIN
    SELECT count(*) INTO n_left
      FROM measures m
      JOIN refs t ON t.ref_id = m.measurand_ref_id
      JOIN measure_unit_compat c
        ON c.legacy_table = t.meta -> 'legacy_source' ->> 'table'
       AND c.legacy_key = t.meta -> 'legacy_source' ->> 'key'
     WHERE m.superseded_by IS NULL
       AND NOT (m.meta ? 'rebased_from')   -- the SI rows just written
       AND t.meta ->> 'canonical_unit' = c.legacy_unit
       AND (m.value_num IS NOT NULL OR m.value_low IS NOT NULL
            OR m.value_high IS NOT NULL OR m.value_err IS NOT NULL);
    IF n_left <> 0 THEN
        RAISE EXCEPTION 'measures SI re-base: % live numeric row(s) were left in a '
            'legacy unit', n_left;
    END IF;
END $$;

-- The same rule insert_measure enforces, held by the table (and kept for good):
-- a numeric value with no reported_unit on a taxon stored in a compat row's SI
-- unit is refused. Closes the migrate-to-restart window (old code could store a
-- unitless "25.4" as 25.4 m) with a loud failure instead of a number off by 1000.
-- The WHEN is pure operators; the compat lookup is in the body. Created here,
-- under the lock held since section 4.
CREATE OR REPLACE FUNCTION precis_measures_unitless_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_canon text;
    v_name  text;
BEGIN
    SELECT t.meta ->> 'canonical_unit', t.meta ->> 'name' INTO v_canon, v_name
      FROM refs t
      JOIN measure_unit_compat c
        ON c.legacy_table = t.meta -> 'legacy_source' ->> 'table'
       AND c.legacy_key = t.meta -> 'legacy_source' ->> 'key'
       AND c.si_unit = t.meta ->> 'canonical_unit'
     WHERE t.ref_id = NEW.measurand_ref_id;
    IF FOUND THEN
        RAISE EXCEPTION
            'no unit given for % on % (canonical %); state the unit',
            quote_literal(NEW.literal),
            quote_literal(coalesce(v_name, 'this measurand')),
            quote_literal(v_canon)
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS measures_unitless_guard ON measures;
CREATE TRIGGER measures_unitless_guard
    BEFORE INSERT ON measures FOR EACH ROW
    WHEN (NEW.reported_unit IS NULL
          AND (NEW.value_num IS NOT NULL OR NEW.value_low IS NOT NULL
               OR NEW.value_high IS NOT NULL OR NEW.value_err IS NOT NULL))
    EXECUTE FUNCTION precis_measures_unitless_guard();

-- Round trip, formula half: for every row written above, the legacy value
-- recomputed from the SI value through the compat row must equal the legacy row's
-- own to 1e-12 relative (NULL against NULL counts as equal, NULL against a number
-- does not). Any failure aborts everything, before a hot-table lock is taken.
DO $$
DECLARE
    bad record;
BEGIN
    SELECT t.ref_id AS taxon, n.id AS new_id, l.id AS old_id, chk.col,
           chk.orig, chk.via_formula
      INTO bad
      FROM measures n
      JOIN measures l ON l.id = n.supersedes
      JOIN refs t ON t.ref_id = n.measurand_ref_id
      JOIN measure_unit_compat c
        ON c.legacy_table = t.meta -> 'legacy_source' ->> 'table'
       AND c.legacy_key = t.meta -> 'legacy_source' ->> 'key'
      CROSS JOIN LATERAL (VALUES
          ('value_num', l.value_num,
           precis_measure_legacy_value(n.value_num, c.factor, c.si_offset)),
          ('value_low', l.value_low,
           precis_measure_legacy_value(n.value_low, c.factor, c.si_offset)),
          ('value_high', l.value_high,
           precis_measure_legacy_value(n.value_high, c.factor, c.si_offset)),
          ('value_err', l.value_err,
           precis_measure_legacy_value(n.value_err, c.factor, 0))
      ) AS chk(col, orig, via_formula)
     WHERE n.actor = 'migration-0188'
       AND ((chk.orig IS NULL) <> (chk.via_formula IS NULL)
            OR abs(chk.via_formula - chk.orig) > 1e-12 * abs(chk.orig))
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION
            'measures SI re-base round trip failed: taxon %, measure % (legacy %), '
            '%: legacy value %, back-converted %',
            bad.taxon, bad.new_id, bad.old_id, bad.col, bad.orig, bad.via_formula;
    END IF;
END $$;

-- ── 6. statements that lock hot tables ─────────────────────────────────

-- display_unit is content of a taxon (a change of it changes what a reader
-- sees), so edits to it reach the revisions log (0185). The kind list is
-- unchanged, so 0185's refresh finds the same WHEN lists and runs no DDL.
UPDATE kinds SET covered_meta = covered_meta || ARRAY['display_unit']
 WHERE slug = 'taxon' AND covered_meta IS NOT NULL
   AND NOT ('display_unit' = ANY (covered_meta));

-- The taxa: canonical_unit becomes SI, display_unit the legacy unit. The unit
-- guard honours the setting only for exactly this change, once.
SELECT set_config('precis.allow_unit_rebase', 'on', true);
UPDATE refs r
   SET meta = r.meta || jsonb_build_object('canonical_unit', c.si_unit,
                                           'display_unit', c.legacy_unit)
  FROM measure_unit_compat c
 WHERE r.kind = 'taxon'
   AND r.meta -> 'legacy_source' ->> 'table' = c.legacy_table
   AND r.meta -> 'legacy_source' ->> 'key' = c.legacy_key
   AND r.meta ->> 'canonical_unit' = c.legacy_unit;
SELECT set_config('precis.allow_unit_rebase', 'off', true);

-- The views read back through the compat table: legacy = (si - offset) / factor,
-- but only for a taxon that is in the compat row's SI unit; a taxon in any other
-- unit passes its numbers through unconverted (a factor must never be applied to
-- a number it was not made for).
-- set_by of a re-based row is the legacy row's writer, so a legacy reader sees
-- what it always saw. Same columns, same order: CREATE OR REPLACE keeps the
-- INSTEAD OF triggers.
CREATE OR REPLACE VIEW material_values AS
SELECT m.id, m.subject_ref_id AS material_ref_id,
       t.meta -> 'legacy_source' ->> 'key' AS property_id,
       precis_measure_legacy_value(m.value_num, c.factor, c.si_offset) AS value_num,
       precis_measure_legacy_value(m.value_low, c.factor, c.si_offset) AS value_low,
       precis_measure_legacy_value(m.value_high, c.factor, c.si_offset) AS value_high,
       m.value_text, m.value_bool,
       m.input_unit, m.conditions, m.maturity, m.method,
       m.source_ref_id, m.source_chunk, m.source_url, m.as_of,
       CASE WHEN m.actor = 'migration-0188' THEN nullif(m.meta ->> 'legacy_actor', 'legacy')
            WHEN m.actor = 'legacy' THEN NULL
            ELSE m.actor END AS set_by,
       m.created_at, m.notes
  FROM measures m
  JOIN refs t ON t.ref_id = m.measurand_ref_id
  LEFT JOIN measure_unit_compat c
    ON c.legacy_table = 'material_properties'
   AND c.legacy_key = t.meta -> 'legacy_source' ->> 'key'
   AND c.si_unit = t.meta ->> 'canonical_unit'
 WHERE m.superseded_by IS NULL
   AND t.meta -> 'legacy_source' ->> 'table' = 'material_properties';

CREATE OR REPLACE VIEW component_spec_values AS
SELECT m.id, m.subject_ref_id AS component_ref_id,
       t.meta -> 'legacy_source' ->> 'key' AS spec_id,
       precis_measure_legacy_value(m.value_num, c.factor, c.si_offset) AS value_num,
       precis_measure_legacy_value(m.value_low, c.factor, c.si_offset) AS value_low,
       precis_measure_legacy_value(m.value_high, c.factor, c.si_offset) AS value_high,
       m.value_text, m.value_bool,
       m.input_unit, m.conditions, m.maturity, m.method,
       m.source_ref_id, m.source_chunk, m.source_url, m.as_of,
       CASE WHEN m.actor = 'migration-0188' THEN nullif(m.meta ->> 'legacy_actor', 'legacy')
            WHEN m.actor = 'legacy' THEN NULL
            ELSE m.actor END AS set_by,
       m.created_at, m.notes
  FROM measures m
  JOIN refs t ON t.ref_id = m.measurand_ref_id
  LEFT JOIN measure_unit_compat c
    ON c.legacy_table = 'component_specs'
   AND c.legacy_key = t.meta -> 'legacy_source' ->> 'key'
   AND c.si_unit = t.meta ->> 'canonical_unit'
 WHERE m.superseded_by IS NULL
   AND t.meta -> 'legacy_source' ->> 'table' = 'component_specs';

COMMENT ON VIEW material_values IS
    'Compatibility view over measures (live rows whose measurand came from material_properties), in the property''s LEGACY unit: values read back through measure_unit_compat. INSERT only, through an INSTEAD OF trigger that converts to SI and synthesises literal and run_key; there is no UPDATE or DELETE door.';
COMMENT ON VIEW component_spec_values IS
    'Compatibility view over measures (live rows whose measurand came from component_specs), in the spec''s LEGACY unit: values read back through measure_unit_compat. INSERT only, through an INSTEAD OF trigger that converts to SI and synthesises literal and run_key; there is no UPDATE or DELETE door.';

-- ── 7. the round-trip check, view half (reads only; a mismatch aborts) ──

-- For every row this migration wrote that a compatibility view shows: the view's
-- value must equal the legacy row's own to 1e-12 relative (NULL against NULL
-- counts as equal). Needs the final state (taxa in SI, views replaced).
DO $$
DECLARE
    bad record;
BEGIN
    SELECT t.ref_id AS taxon, n.id AS new_id, l.id AS old_id, chk.col,
           chk.orig, chk.via_view
      INTO bad
      FROM measures n
      JOIN measures l ON l.id = n.supersedes
      JOIN refs t ON t.ref_id = n.measurand_ref_id
      JOIN (SELECT id, value_num, value_low, value_high FROM material_values
            UNION ALL
            SELECT id, value_num, value_low, value_high
              FROM component_spec_values) v ON v.id = n.id
      CROSS JOIN LATERAL (VALUES
          ('value_num', l.value_num, v.value_num),
          ('value_low', l.value_low, v.value_low),
          ('value_high', l.value_high, v.value_high)
      ) AS chk(col, orig, via_view)
     WHERE n.actor = 'migration-0188'
       AND ((chk.orig IS NULL) <> (chk.via_view IS NULL)
            OR abs(chk.via_view - chk.orig) > 1e-12 * abs(chk.orig))
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION
            'measures SI re-base view round trip failed: taxon %, measure % '
            '(legacy %), %: legacy value %, view %',
            bad.taxon, bad.new_id, bad.old_id, bad.col, bad.orig, bad.via_view;
    END IF;
END $$;

-- rxn_values: proven empty at the top of this file (and locked since). Reaction
-- values are plain measures rows; no compatibility view (the one reader is
-- ported). Dropping a table that references refs locks refs: last, so the lock
-- is held for the drop only.
-- squawk-ignore ban-drop-table
DROP TABLE rxn_values;

COMMIT;

-- End of 0188_measures_si.sql
