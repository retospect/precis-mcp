-- 0236_se_charge_dipole_si.sql
--
-- Ontology-v2 slice 0 (Reto, 2026-10-22): the two SE measurands 0182 seeded
-- in a scaled unit move to coherent SI, the 0188 way. "Net partial charge"
-- (`net-partial-charge`) was stored in elementary charges (`e`), "Dipole
-- moment" (`dipole-moment`) in debye (`D`). The taxon unit guard of slice 0a
-- refuses a canonical_unit that is not the si_vector's coherent unit, so
-- they become `C` and `C*m`, with `display_unit` the old unit.
--
-- `measure_unit_compat` was keyed by a legacy registry only
-- (meta.legacy_source). These taxa have none, so the table gains a fourth
-- key space, `('taxon', <slug>)`, for a taxon with no legacy_source. Two
-- helpers spell the key a taxon's meta has (precis_measure_compat_table /
-- _key), and the two guards that look a taxon's compat row up use them:
--
--   * precis_taxon_unit_guard: the one-way re-base bypass (0188) now also
--     matches a ('taxon', slug) row; the slug must not change in the same
--     update, as legacy_source must not;
--   * precis_measures_unitless_guard: a bare number on such a taxon (now in
--     SI) is refused, as 0188 does for the legacy taxa.
--
-- The compatibility views and precis_legacy_value_insert stay keyed by
-- legacy_source (they serve the legacy registries only).
--
-- Live measures: every live numeric row on a taxon still in the old unit is
-- superseded by an SI row (actor 'migration-0236'), exactly as 0188 did. The
-- supersede scans all live rows at apply time, so a measure written between
-- this commit and the deploy is converted too (prod on 2026-10-22: one live
-- row on net-partial-charge, none on dipole-moment).
--
-- Factors are exact: e = 1.602176634e-19 C (SI 2019, exact);
-- D = 1e-21 / c C*m = 3.335640951981520495755767145e-30 (pint agrees).
--
-- Idempotent: a replay finds the taxa already in SI and converts nothing.
-- Forward-only (ADR 0005).

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ── 1. compat key space for non-legacy taxa ────────────────────────────

-- Widens the CHECK by one value; measure_unit_compat is a ~30-row seed
-- table, so the validating scan is instant and the old guarantee is kept.
-- squawk-ignore ban-drop-constraint
ALTER TABLE measure_unit_compat DROP CONSTRAINT IF EXISTS measure_unit_compat_legacy_table_check;
ALTER TABLE measure_unit_compat ADD CONSTRAINT measure_unit_compat_legacy_table_check
    CHECK (legacy_table IN ('material_properties', 'component_specs',
                            'rxn_properties', 'taxon')) NOT VALID;
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE measure_unit_compat VALIDATE CONSTRAINT measure_unit_compat_legacy_table_check;

COMMENT ON TABLE measure_unit_compat IS
    'Units a taxon was kept in before it moved to coherent SI, and the SI unit it stores now: si = legacy * factor + si_offset, computed in numeric (factor and offset are exact decimals) and cast to float8, so 25.4 mm is exactly 0.0254 m. Keyed by the taxon''s stable identifier, never its ref_id: (table, key) of meta.legacy_source for a legacy taxon (seeded by 0174, re-based by 0188), (''taxon'', meta.slug) for a taxon with no legacy_source (0236). Read by the material_values / component_spec_values views and their insert triggers (legacy keys only), by precis_measure_taxon (a minted legacy taxon starts in SI), by the unit and unitless guards and by the store''s writers. Seed vocabulary: it rides in the baseline.';

INSERT INTO measure_unit_compat
    (legacy_table, legacy_key, legacy_unit, si_unit, factor) VALUES
    ('taxon', 'net-partial-charge', 'e', 'C', 1.602176634e-19),
    ('taxon', 'dipole-moment',      'D', 'C*m', 3.335640951981520495755767145e-30)
ON CONFLICT (legacy_table, legacy_key) DO NOTHING;

-- ── 2. functions (catalog only; no table locks) ────────────────────────

-- The compat key a taxon's meta has: its legacy_source for a legacy taxon,
-- else ('taxon', slug).
CREATE OR REPLACE FUNCTION precis_measure_compat_table(p_meta jsonb)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN jsonb_typeof(p_meta -> 'legacy_source') = 'object'
                THEN p_meta -> 'legacy_source' ->> 'table'
                ELSE 'taxon' END
$$;

CREATE OR REPLACE FUNCTION precis_measure_compat_key(p_meta jsonb)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN jsonb_typeof(p_meta -> 'legacy_source') = 'object'
                THEN p_meta -> 'legacy_source' ->> 'key'
                ELSE p_meta ->> 'slug' END
$$;

-- The unit guard (0188) with the bypass matched through the helpers.
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
           AND OLD.meta -> 'slug' IS NOT DISTINCT FROM NEW.meta -> 'slug'
           AND EXISTS (SELECT 1 FROM measure_unit_compat c
                        WHERE c.legacy_table = precis_measure_compat_table(OLD.meta)
                          AND c.legacy_key = precis_measure_compat_key(OLD.meta)
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

-- The unitless guard (0188) with the compat row matched through the helpers.
CREATE OR REPLACE FUNCTION precis_measures_unitless_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_canon text;
    v_name  text;
BEGIN
    SELECT t.meta ->> 'canonical_unit', t.meta ->> 'name' INTO v_canon, v_name
      FROM refs t
      JOIN measure_unit_compat c
        ON c.legacy_table = precis_measure_compat_table(t.meta)
       AND c.legacy_key = precis_measure_compat_key(t.meta)
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

-- ── 3. scans ───────────────────────────────────────────────────────────

-- From here to COMMIT no measure can be written or changed by another session.
LOCK TABLE measures IN SHARE ROW EXCLUSIVE MODE;

-- A ('taxon', slug) key must name one live taxon: a slug shared by two would
-- re-base both with one factor.
DO $$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(format('%s (%s taxa)', c.legacy_key, n), ', ')
      INTO bad
      FROM measure_unit_compat c
      JOIN LATERAL (
          SELECT count(*) AS n FROM refs t
           WHERE t.kind = 'taxon' AND t.retired_at IS NULL
             AND precis_measure_compat_table(t.meta) = 'taxon'
             AND t.meta ->> 'slug' = c.legacy_key) k ON true
     WHERE c.legacy_table = 'taxon' AND k.n > 1;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'measures SI re-base (0236): slug(s) % name more than one live taxon; '
            'retire or rename the extra one and re-run', bad;
    END IF;
END $$;

-- Every such taxon is in the old unit (about to be converted) or already SI.
DO $$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(format('%s (%s in %s)', t.ref_id, c.legacy_key,
                             coalesce(t.meta ->> 'canonical_unit', 'no unit')), ', ')
      INTO bad
      FROM refs t
      JOIN measure_unit_compat c
        ON c.legacy_table = 'taxon'
       AND precis_measure_compat_table(t.meta) = 'taxon'
       AND c.legacy_key = t.meta ->> 'slug'
     WHERE t.kind = 'taxon' AND t.retired_at IS NULL
       AND t.meta ->> 'canonical_unit' IS DISTINCT FROM c.legacy_unit
       AND t.meta ->> 'canonical_unit' IS DISTINCT FROM c.si_unit;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'measures SI re-base (0236): taxon(s) % have a canonical_unit that is '
            'neither the old unit nor SI in measure_unit_compat; fix the taxon (or '
            'the compat row) and re-run', bad;
    END IF;
END $$;

-- A taxon about to be re-based must not carry a different display_unit.
DO $$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(format('%s (%s: display_unit %s, compat legacy_unit %s)',
                             t.ref_id, c.legacy_key, t.meta ->> 'display_unit',
                             c.legacy_unit), ', ')
      INTO bad
      FROM refs t
      JOIN measure_unit_compat c
        ON c.legacy_table = 'taxon'
       AND precis_measure_compat_table(t.meta) = 'taxon'
       AND c.legacy_key = t.meta ->> 'slug'
     WHERE t.kind = 'taxon' AND t.retired_at IS NULL
       AND t.meta ->> 'canonical_unit' = c.legacy_unit
       AND t.meta ->> 'display_unit' IS NOT NULL
       AND t.meta ->> 'display_unit' IS DISTINCT FROM c.legacy_unit;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'measures SI re-base (0236): taxon(s) % already have a different '
            'display_unit; clear it (or fix the compat row) and re-run', bad;
    END IF;
END $$;

-- Superseded history in the old unit cannot be rewritten.
DO $$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(format('%s (%s: %s superseded row(s))', t.ref_id,
                             c.legacy_key, s.n), ', ')
      INTO bad
      FROM (SELECT measurand_ref_id, count(*) AS n FROM measures
             WHERE superseded_by IS NOT NULL GROUP BY measurand_ref_id) s
      JOIN refs t ON t.ref_id = s.measurand_ref_id
      JOIN measure_unit_compat c
        ON c.legacy_table = 'taxon'
       AND precis_measure_compat_table(t.meta) = 'taxon'
       AND c.legacy_key = t.meta ->> 'slug'
     WHERE t.retired_at IS NULL
       AND t.meta ->> 'canonical_unit' = c.legacy_unit;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'measures SI re-base (0236): taxon(s) % already hold superseded '
            'measures in the old unit, which a supersession cannot rewrite; '
            'resolve them first', bad;
    END IF;
END $$;

-- ── 4. the SI rows: one new row per live numeric old-unit row ──────────

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
       coalesce(l.reported_unit, c.legacy_unit),
       precis_measure_si_value(l.value_num, c.factor, c.si_offset),
       precis_measure_si_value(l.value_low, c.factor, c.si_offset),
       precis_measure_si_value(l.value_high, c.factor, c.si_offset),
       l.value_text, l.value_bool,
       precis_measure_si_value(l.value_err, c.factor, 0),
       l.value_form, l.reference, l.tier, l.trusted, l.extraction_status,
       l.normalization, l.normalization_status, l.source_attribution,
       l.measurand_status, l.direction, l.role, l.run_key, l.subject,
       l.subject_group, l.experiment_ref_id, l.derived_from, l.primary_link_id,
       l.anchor_scheme, l.span, l.id, 'migration-0236', l.model,
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
    ON c.legacy_table = 'taxon'
   AND precis_measure_compat_table(t.meta) = 'taxon'
   AND c.legacy_key = t.meta ->> 'slug'
 WHERE l.superseded_by IS NULL
   AND t.retired_at IS NULL
   AND t.meta ->> 'canonical_unit' = c.legacy_unit
   AND (l.value_num IS NOT NULL OR l.value_low IS NOT NULL
        OR l.value_high IS NOT NULL OR l.value_err IS NOT NULL)
 ORDER BY l.id;

UPDATE measures l
   SET superseded_by = n.id, superseded_at = now()
  FROM measures n
 WHERE n.supersedes = l.id AND n.actor = 'migration-0236'
   AND l.superseded_by IS NULL;

DO $$
DECLARE
    n_left bigint;
BEGIN
    SELECT count(*) INTO n_left
      FROM measures m
      JOIN refs t ON t.ref_id = m.measurand_ref_id
      JOIN measure_unit_compat c
        ON c.legacy_table = 'taxon'
       AND precis_measure_compat_table(t.meta) = 'taxon'
       AND c.legacy_key = t.meta ->> 'slug'
     WHERE m.superseded_by IS NULL
       AND NOT (m.meta ? 'rebased_from')
       AND t.retired_at IS NULL
       AND t.meta ->> 'canonical_unit' = c.legacy_unit
       AND (m.value_num IS NOT NULL OR m.value_low IS NOT NULL
            OR m.value_high IS NOT NULL OR m.value_err IS NOT NULL);
    IF n_left <> 0 THEN
        RAISE EXCEPTION 'measures SI re-base (0236): % live numeric row(s) were '
            'left in the old unit', n_left;
    END IF;
END $$;

-- Round trip: every SI row written here converts back to its old row's value
-- to 1e-12 relative (NULL against NULL is equal, NULL against a number is not).
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
        ON c.legacy_table = 'taxon'
       AND precis_measure_compat_table(t.meta) = 'taxon'
       AND c.legacy_key = t.meta ->> 'slug'
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
     WHERE n.actor = 'migration-0236'
       AND ((chk.orig IS NULL) <> (chk.via_formula IS NULL)
            OR abs(chk.via_formula - chk.orig) > 1e-12 * abs(chk.orig))
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION
            'measures SI re-base (0236) round trip failed: taxon %, measure % '
            '(old %), %: old value %, back-converted %',
            bad.taxon, bad.new_id, bad.old_id, bad.col, bad.orig, bad.via_formula;
    END IF;
END $$;

-- ── 5. the taxa: canonical_unit becomes SI, display_unit the old unit ──

SELECT set_config('precis.allow_unit_rebase', 'on', true);
UPDATE refs r
   SET meta = r.meta || jsonb_build_object('canonical_unit', c.si_unit,
                                           'display_unit', c.legacy_unit)
  FROM measure_unit_compat c
 WHERE r.kind = 'taxon' AND r.retired_at IS NULL
   AND c.legacy_table = 'taxon'
   AND precis_measure_compat_table(r.meta) = 'taxon'
   AND c.legacy_key = r.meta ->> 'slug'
   AND r.meta ->> 'canonical_unit' = c.legacy_unit;
SELECT set_config('precis.allow_unit_rebase', 'off', true);

COMMIT;
