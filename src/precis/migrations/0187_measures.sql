-- 0187_measures.sql
--
-- The measures substrate (docs/backlog/measures-substrate.md, "Pilot build on
-- qu202467", Build A; Reto knowledge-mesh-12, 2026-10-03). One sourced-number
-- record for every subject ref:
--
--   * `material_values` is RENAMED to `measures` and widened. `component_spec_
--     values` folds into it (option ii of the decisions log). Both old names
--     stay as VIEWS over `measures` (live rows only) with INSTEAD OF INSERT
--     triggers, so `store/_material_ops.py` and `store/_component_ops.py` run
--     unchanged. There is deliberately NO INSTEAD OF UPDATE or DELETE: no store
--     op issues one (grep of src/precis/store is empty). Material value ids are
--     kept; component value ids are renumbered (no stored consumer: a grep of
--     src/ and tests/ finds none) and only run_key 'legacy:component:<oldid>'
--     keeps the old id.
--
--   * The measurand is a `kind='taxon'` ref (`measurand_ref_id`), not a row in
--     a registry table. Legacy rows map through the taxon's
--     `meta.legacy_source = {table, key}` seeded by 0174. A property registered
--     AFTER 0174 through the legacy mint verbs has no taxon yet:
--     `precis_measure_taxon(..., true)` mints one, the same shape 0174 writes
--     (the backfill below and the compatibility views' insert triggers both
--     call it, and it RAISEs a NOTICE naming each taxon it mints). Read-only
--     prod check, 2026-10-03: 24/24 material_properties and 30/30
--     component_specs rows already map to a 0174 taxon, so today nothing is
--     minted. The mint takes an advisory lock per (table, key) and re-checks, so
--     two concurrent mints cannot both insert. The lookup runs over
--     kind='taxon' rows only and needs no index of its own (refs_kind_idx
--     serves it).
--
--   * Rows are append-only: no UPDATE, no DELETE, no TRUNCATE. `measures_frozen`
--     refuses every UPDATE except
--       - `trusted`, and `extraction_status` -> 'human_checked';
--       - a first `superseded_by` + `superseded_at` together (the target must
--         be another LIVE row with the same subject, subject label, measurand
--         and direction);
--       - `primary_link_id` / `source_ref_id` going NULL once the link / ref no
--         longer exists (the foreign keys' own ON DELETE SET NULL).
--     `meta` is frozen; `meta.extra_anchors` is written at insert only. A change
--     is a new row plus a `supersedes` pointer. BEFORE DELETE and BEFORE
--     TRUNCATE triggers refuse naming the rule (a TRUNCATE honours the session
--     setting precis.allow_measures_truncate = 'on', which only test cleanup
--     sets). The subject FK, inherited as ON DELETE CASCADE, becomes RESTRICT:
--     a subject or measurand ref with measures cannot be hard-deleted (same
--     shape as the merge guard in `LinksMixin.migrate_links`, which counts
--     superseded rows too and so is conservative). `cli/maintenance.py` purge
--     skips such refs. "Live" = `superseded_by IS NULL`, covered by partial
--     indexes.
--
--   * A run is one output row plus its own input rows (`direction = 'input'`),
--     tied by `run_key`. `experiment_ref_id` ships NULL; the experiment kind
--     fills it from `run_key` one to one when it exists.
--
--   * New relation `quantifies` / `quantified-by`: paper chunk -> measurand
--     taxon, a normal deduplicated link (written through add_link). The
--     anchor itself lives on the measure row (`anchor_scheme` + `span`
--     jsonb), so two measures on the same chunk and measurand share one edge
--     and each keeps its own span. `measures.primary_link_id` points at the
--     edge; further anchors go in `meta.extra_anchors` at insert.
--
--   * `reviews.target_kind` gains 'measure'; `precis_target_sha('measure', id)`
--     hashes an explicit, ordered list of the frozen columns (text, float bits,
--     canonical jsonb; no timestamptz), so it is independent of the session's
--     TimeZone, DateStyle and extra_float_digits. An annotation update never
--     stales a review and a stale measure review is an invariant alarm.
--
--   * A trigger on refs refuses to change a taxon's `canonical_unit`,
--     `dimension_kind` or `si_vector` while the taxon has live measures: values
--     are stored normalised to them, so a silent change would re-base them.
--     canonical_unit is refused for any change, NULL -> value included:
--     `insert_measure` refuses a number with a `reported_unit` on a taxon that
--     has no canonical_unit (one unit per measurand, values stored normalised),
--     but accepts a unitless number there, so filling the unit in later would
--     give those numbers a unit they were never written in. dimension_kind / si_vector may go NULL -> value
--     (filling in an unmapped dimension) but not value -> another value.
--     `precis_measure_taxon` copies the legacy registry's unit into
--     canonical_unit when it mints, so a minted legacy taxon starts without a
--     unit only when its source has none. (The taxon handler has no edit path
--     to guard, so the rule lives where every writer meets it.)
--
-- Lock order: every statement that takes more than a row lock on refs, links
-- or chunks (DROP TABLE of a table that references refs, ADD CONSTRAINT ...
-- REFERENCES, CREATE TRIGGER ON refs, UPDATE kinds) is at the end of the file,
-- after every scan and backfill. The only scan left under those locks is the
-- FK validation of `measures` itself.
--
-- Forward-only (ADR 0005). One transaction.

BEGIN;

-- ── 1. the relation ────────────────────────────────────────────────────

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('quantifies',    FALSE, 'quantified-by',
     'Source paper (chunk-scoped) states a number for the target measurand taxon. One shared edge per (chunk, measurand); each measure keeps its own anchor_scheme + span on its row, and measures.primary_link_id points at the edge.'),
    ('quantified-by', FALSE, 'quantifies',
     'Source measurand taxon is quantified by the target paper chunk.')
ON CONFLICT (slug) DO NOTHING;

-- ── 2. taxon lookup through the legacy registries ──────────────────────

-- The taxon seeded (0174) for one legacy registry row; NULL when none. With
-- p_create, a row that exists in the legacy registry but has no taxon (a
-- property proposed after 0174) gets one, the same shape 0174 writes:
-- name/norm_name/slug/definition/aliases/status + the legacy dimension facts
-- that need no lookup table, an ord -1 card_combined chunk (embedding NULL:
-- the worker embeds), and a `specialises` edge to the `measurand` start node.
CREATE OR REPLACE FUNCTION precis_measure_taxon(
    p_table text, p_key text, p_create boolean DEFAULT FALSE)
    RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    v_src   jsonb := jsonb_build_object('table', p_table, 'key', p_key);
    v_id    bigint;
    v_name  text;
    v_unit  text;
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

-- The printed form of a legacy value (the literal backfill rule, and what the
-- views' insert triggers synthesise for a legacy writer that names no literal).
CREATE OR REPLACE FUNCTION precis_measure_literal(
    p_num double precision, p_low double precision, p_high double precision,
    p_text text, p_bool boolean)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_text IS NOT NULL THEN p_text
        WHEN p_num IS NOT NULL THEN p_num::text
        WHEN p_low IS NOT NULL AND p_high IS NOT NULL THEN p_low::text || '–' || p_high::text
        WHEN p_low IS NOT NULL THEN '≥' || p_low::text
        WHEN p_high IS NOT NULL THEN '≤' || p_high::text
        WHEN p_bool IS NOT NULL THEN p_bool::text
        ELSE '(none)' END
$$;

CREATE OR REPLACE FUNCTION precis_measure_form(
    p_num double precision, p_low double precision, p_high double precision,
    p_text text, p_bool boolean)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_text IS NOT NULL THEN 'categorical'
        WHEN p_bool IS NOT NULL THEN 'boolean'
        WHEN p_num IS NOT NULL THEN 'point'
        WHEN p_low IS NOT NULL AND p_high IS NOT NULL THEN 'interval'
        WHEN p_low IS NOT NULL THEN 'lower_bound'
        WHEN p_high IS NOT NULL THEN 'upper_bound'
        ELSE 'not_established' END
$$;

-- ── 3. rename, widen ───────────────────────────────────────────────────

-- squawk-ignore renaming-table
ALTER TABLE material_values RENAME TO measures;
ALTER TABLE measures RENAME COLUMN material_ref_id TO subject_ref_id;
ALTER SEQUENCE material_values_id_seq RENAME TO measures_id_seq;
ALTER INDEX material_values_pkey RENAME TO measures_pkey;
ALTER INDEX material_values_material_idx RENAME TO measures_subject_idx;

-- literal / measurand_ref_id / run_key / actor are NOT NULL in the end state;
-- they carry a throwaway default here so the column adds are metadata-only,
-- and the defaults are dropped once the backfill below has filled them.
ALTER TABLE measures
    ADD COLUMN literal text NOT NULL DEFAULT '',
    ADD COLUMN reported_unit text,
    ADD COLUMN value_form text
        CHECK (value_form IN ('point', 'approximate_point', 'upper_bound',
               'lower_bound', 'interval', 'categorical', 'boolean',
               'not_established')),
    ADD COLUMN value_err double precision,
    ADD COLUMN reference text,
    ADD COLUMN tier text
        CHECK (tier IN ('measured', 'computed', 'derived', 'asserted')),
    ADD COLUMN trusted boolean,
    ADD COLUMN extraction_status text NOT NULL DEFAULT 'unverified'
        CHECK (extraction_status IN ('unverified', 'anchor_matched',
               'anchor_mismatch', 'human_checked')),
    ADD COLUMN normalization text,
    ADD COLUMN normalization_status text
        CHECK (normalization_status IN ('explicit', 'inferred', 'unresolved')),
    ADD COLUMN source_attribution text
        CHECK (source_attribution IN ('own_work', 'cited_work', 'not_established')),
    ADD COLUMN measurand_status text
        CHECK (measurand_status IN ('explicit', 'interpreted', 'ambiguous')),
    ADD COLUMN measurand_ref_id bigint NOT NULL DEFAULT 0,
    ADD COLUMN direction text NOT NULL DEFAULT 'output'
        CHECK (direction IN ('input', 'output', 'covariate')),
    ADD COLUMN role text
        CHECK (role IN ('context', 'preparation', 'model')),
    ADD COLUMN run_key text NOT NULL DEFAULT '',
    ADD COLUMN subject text,
    ADD COLUMN subject_group text,
    ADD COLUMN experiment_ref_id bigint,
    ADD COLUMN derived_from bigint[],
    ADD COLUMN primary_link_id bigint,
    ADD COLUMN anchor_scheme text,
    ADD COLUMN span jsonb,
    ADD COLUMN supersedes bigint,
    ADD COLUMN superseded_by bigint,
    ADD COLUMN superseded_at timestamptz,
    ADD COLUMN actor text NOT NULL DEFAULT '',
    ADD COLUMN model text,
    ADD COLUMN meta jsonb NOT NULL DEFAULT '{}'::jsonb;

-- ── 4. backfill the existing material rows ─────────────────────────────

UPDATE measures m
   SET measurand_ref_id = coalesce(
               precis_measure_taxon('material_properties', m.property_id, TRUE), 0),
       literal = precis_measure_literal(m.value_num, m.value_low, m.value_high,
                                        m.value_text, m.value_bool),
       value_form = precis_measure_form(m.value_num, m.value_low, m.value_high,
                                        m.value_text, m.value_bool),
       run_key = 'legacy:' || m.id,
       actor = coalesce(nullif(btrim(m.set_by), ''), 'legacy');

DO $$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(DISTINCT m.property_id, ', ') INTO bad
      FROM measures m WHERE m.measurand_ref_id = 0;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'measures backfill: material_properties row(s) % map to no taxon and '
            'none could be minted (no such registry row)', bad;
    END IF;
END $$;

-- squawk-ignore ban-drop-column
ALTER TABLE measures DROP COLUMN property_id;
-- squawk-ignore ban-drop-column
ALTER TABLE measures DROP COLUMN set_by;

-- ── 5. fold component_spec_values in (subject = the component ref) ─────

DO $$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(DISTINCT c.spec_id, ', ') INTO bad
      FROM component_spec_values c
     WHERE precis_measure_taxon('component_specs', c.spec_id, TRUE) IS NULL;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'measures backfill: component_specs row(s) % map to no taxon and '
            'none could be minted (no such registry row)', bad;
    END IF;
END $$;

INSERT INTO measures
    (subject_ref_id, measurand_ref_id, value_num, value_low, value_high,
     value_text, value_bool, input_unit, conditions, maturity, method,
     source_ref_id, source_chunk, source_url, as_of, created_at, notes,
     literal, value_form, run_key, actor)
SELECT c.component_ref_id, precis_measure_taxon('component_specs', c.spec_id, TRUE),
       c.value_num, c.value_low, c.value_high, c.value_text, c.value_bool,
       c.input_unit, c.conditions, c.maturity, c.method, c.source_ref_id,
       c.source_chunk, c.source_url, c.as_of, c.created_at, c.notes,
       precis_measure_literal(c.value_num, c.value_low, c.value_high,
                              c.value_text, c.value_bool),
       precis_measure_form(c.value_num, c.value_low, c.value_high,
                           c.value_text, c.value_bool),
       'legacy:component:' || c.id,
       coalesce(nullif(btrim(c.set_by), ''), 'legacy')
  FROM component_spec_values c
 ORDER BY c.id;

DO $$
DECLARE
    n_old bigint;
    n_new bigint;
BEGIN
    SELECT count(*) INTO n_old FROM component_spec_values;
    SELECT count(*) INTO n_new FROM measures WHERE run_key LIKE 'legacy:component:%';
    IF n_old <> n_new THEN
        RAISE EXCEPTION 'measures fold-in: component_spec_values has % rows but % landed',
            n_old, n_new;
    END IF;
END $$;

-- ── 6. constraints and indexes on measures itself ──────────────────────

ALTER TABLE measures
    ALTER COLUMN literal DROP DEFAULT,
    ALTER COLUMN measurand_ref_id DROP DEFAULT,
    ALTER COLUMN run_key DROP DEFAULT,
    ALTER COLUMN actor DROP DEFAULT;

ALTER TABLE measures ADD CONSTRAINT measures_actor_check
    CHECK (btrim(actor) <> '') NOT VALID;
-- measures is a handful of rows; a validating scan in the single-writer window costs nothing
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE measures VALIDATE CONSTRAINT measures_actor_check;

-- Live rows only: the partial index every reader (best_measure, the views,
-- measures_for) goes through.
CREATE INDEX measures_live_measurand_idx
    ON measures (measurand_ref_id, value_num) WHERE superseded_by IS NULL;
CREATE INDEX measures_live_subject_idx
    ON measures (subject_ref_id, measurand_ref_id) WHERE superseded_by IS NULL;
CREATE INDEX measures_run_key_idx ON measures (run_key);
CREATE INDEX measures_primary_link_idx
    ON measures (primary_link_id) WHERE primary_link_id IS NOT NULL;
CREATE INDEX measures_measurand_idx ON measures (measurand_ref_id);
CREATE INDEX measures_experiment_idx
    ON measures (experiment_ref_id) WHERE experiment_ref_id IS NOT NULL;
CREATE INDEX measures_supersedes_idx
    ON measures (supersedes) WHERE supersedes IS NOT NULL;
CREATE INDEX measures_superseded_by_idx
    ON measures (superseded_by) WHERE superseded_by IS NOT NULL;

COMMENT ON TABLE measures IS
    'One sourced number per row (measures-substrate.md). Append-only: no DELETE or '
    'TRUNCATE, and a trigger refuses every UPDATE but the annotations; a change is '
    'a new row + supersedes. measurand_ref_id is a taxon ref and subject_ref_id any '
    'ref (handler-enforced; refs has no per-kind FK). Values are stored in the '
    'measurand taxon''s canonical_unit; literal + reported_unit keep what was '
    'printed. A run = one output row + its direction=input rows, tied by '
    'run_key. Reads of "current" numbers filter superseded_by IS NULL.';
COMMENT ON COLUMN measures.literal IS
    'The exact reported string; the parsed value_* columns are the derived reading.';
COMMENT ON COLUMN measures.reference IS
    'Reference state / convention (RHE, SHE, Ag/AgCl, ...): an input to the number, never folded into the unit.';
COMMENT ON COLUMN measures.tier IS
    'measured | computed | derived | asserted; NULL = could not establish (never defaulted to asserted).';
COMMENT ON COLUMN measures.trusted IS
    'Denormalised read of findings-derived trust; NULL = unassessed. Annotation, not content.';
COMMENT ON COLUMN measures.extraction_status IS
    'Computed on write: does the literal occur in the anchored span? human_checked is the one value an UPDATE may set.';
COMMENT ON COLUMN measures.normalization IS
    'Normalisation basis (per catalyst mass, per geometric area, per ECSA ...); not the same axis as reference.';
COMMENT ON COLUMN measures.run_key IS
    'Joins an output to exactly its own input rows. One key per claim x condition set; the compatibility views use legacy:<id>.';
COMMENT ON COLUMN measures.experiment_ref_id IS
    'Reserved: the experiment kind fills it from run_key one to one when it ships.';
COMMENT ON COLUMN measures.primary_link_id IS
    'The quantifies edge (paper chunk -> measurand taxon) that anchors this row; shared by every measure on that chunk and measurand. NULL on a measured row = anchor lost (the chunk or paper was deleted).';
COMMENT ON COLUMN measures.span IS
    'Where in the anchored chunk the number is printed (a string, or [chunk, start, end] raw offsets); anchor_scheme names the form. Further anchors: meta.extra_anchors, written at insert only.';
COMMENT ON COLUMN measures.actor IS
    'Who wrote the row (same pair as reviews: actor + model); a legacy NULL set_by became ''legacy''.';

-- ── 7. append-only: freeze everything but the annotations ──────────────

CREATE OR REPLACE FUNCTION precis_measures_frozen()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    k text;
    -- a first supersession: both columns set together, onto another row that
    -- states the same number (same subject, measurand and direction)
    v_sup_ok boolean :=
        OLD.superseded_by IS NULL AND OLD.superseded_at IS NULL
        AND NEW.superseded_by IS NOT NULL AND NEW.superseded_at IS NOT NULL
        AND NEW.superseded_by <> OLD.id
        AND EXISTS (SELECT 1 FROM measures t
                     WHERE t.id = NEW.superseded_by
                       AND t.subject_ref_id = OLD.subject_ref_id
                       AND t.subject IS NOT DISTINCT FROM OLD.subject
                       AND t.measurand_ref_id = OLD.measurand_ref_id
                       AND t.direction = OLD.direction
                       AND t.superseded_by IS NULL);
    -- the foreign keys' own ON DELETE SET NULL: only once the target is gone
    v_link_ok boolean :=
        OLD.primary_link_id IS NOT NULL AND NEW.primary_link_id IS NULL
        AND NOT EXISTS (SELECT 1 FROM links WHERE link_id = OLD.primary_link_id);
    v_src_ok boolean :=
        OLD.source_ref_id IS NOT NULL AND NEW.source_ref_id IS NULL
        AND NOT EXISTS (SELECT 1 FROM refs WHERE ref_id = OLD.source_ref_id);
BEGIN
    FOR k IN
        SELECT coalesce(n.key, o.key)
          FROM jsonb_each(to_jsonb(NEW)) n
          FULL JOIN jsonb_each(to_jsonb(OLD)) o ON o.key = n.key
         WHERE n.value IS DISTINCT FROM o.value
           AND coalesce(n.key, o.key) NOT IN ('trusted', 'extraction_status')
           AND NOT (coalesce(n.key, o.key) IN ('superseded_by', 'superseded_at')
                    AND v_sup_ok)
           AND NOT (coalesce(n.key, o.key) = 'primary_link_id' AND v_link_ok)
           AND NOT (coalesce(n.key, o.key) = 'source_ref_id' AND v_src_ok)
    LOOP
        RAISE EXCEPTION
            'measures are append-only: column % of measure % is frozen '
            '(only trusted, extraction_status -> human_checked, a first '
            'superseded_by + superseded_at onto a row with the same subject, '
            'measurand and direction, and a primary_link_id / source_ref_id '
            'whose target is gone may change; supersede with a new row)',
            k, OLD.id USING ERRCODE = 'check_violation';
    END LOOP;
    IF NEW.extraction_status IS DISTINCT FROM OLD.extraction_status
       AND NEW.extraction_status <> 'human_checked' THEN
        RAISE EXCEPTION
            'measures are append-only: extraction_status of measure % may only '
            'be set to human_checked (computed on write, never asserted)', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS measures_frozen ON measures;
CREATE TRIGGER measures_frozen
    BEFORE UPDATE ON measures FOR EACH ROW
    EXECUTE FUNCTION precis_measures_frozen();

-- No DELETE: a delete + insert under the same id would rewrite frozen values
-- beneath a review. No TRUNCATE either, except for test cleanup.
CREATE OR REPLACE FUNCTION precis_measures_no_delete()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        IF coalesce(current_setting('precis.allow_measures_truncate', true), '') = 'on' THEN
            RETURN NULL;
        END IF;
        RAISE EXCEPTION 'measures are append-only: TRUNCATE is refused'
            USING ERRCODE = 'check_violation';
    END IF;
    RAISE EXCEPTION 'measures are append-only: measure % cannot be deleted '
        '(supersede it with a new row)', OLD.id
        USING ERRCODE = 'check_violation';
END
$$;

DROP TRIGGER IF EXISTS measures_no_delete ON measures;
CREATE TRIGGER measures_no_delete
    BEFORE DELETE ON measures FOR EACH ROW
    EXECUTE FUNCTION precis_measures_no_delete();
DROP TRIGGER IF EXISTS measures_no_truncate ON measures;
CREATE TRIGGER measures_no_truncate
    BEFORE TRUNCATE ON measures FOR EACH STATEMENT
    EXECUTE FUNCTION precis_measures_no_delete();

-- A taxon's canonical_unit / dimension_kind / si_vector are what its stored
-- values are normalised to.
-- canonical_unit: any change is refused while live measures exist, NULL -> value
-- included (insert_measure accepts a unitless number on a unit-less taxon, so
-- filling the unit in later would give those numbers a unit they never had).
-- dimension_kind / si_vector: NULL -> value is allowed (a curator filling in an
-- unmapped dimension); value -> another value or -> NULL is refused.
CREATE OR REPLACE FUNCTION precis_taxon_unit_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_changed boolean :=
        OLD.meta -> 'canonical_unit' IS DISTINCT FROM NEW.meta -> 'canonical_unit'
        OR (OLD.meta -> 'dimension_kind' IS DISTINCT FROM NEW.meta -> 'dimension_kind'
            AND coalesce(OLD.meta -> 'dimension_kind', 'null'::jsonb) <> 'null'::jsonb)
        OR (OLD.meta -> 'si_vector' IS DISTINCT FROM NEW.meta -> 'si_vector'
            AND coalesce(OLD.meta -> 'si_vector', 'null'::jsonb) <> 'null'::jsonb);
BEGIN
    IF v_changed AND EXISTS (SELECT 1 FROM measures
                WHERE measurand_ref_id = OLD.ref_id AND superseded_by IS NULL) THEN
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

-- ── 8. the ledger's fourth target ──────────────────────────────────────

ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_target_kind_check;
ALTER TABLE reviews ADD CONSTRAINT reviews_target_kind_check
    CHECK (target_kind IN ('chunk', 'ref', 'link', 'measure')) NOT VALID;
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE reviews VALIDATE CONSTRAINT reviews_target_kind_check;

-- One sha part: NULL-safe, unambiguous, independent of session settings.
CREATE OR REPLACE FUNCTION precis_sha_part(p text)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT quote_nullable(p) $$;
CREATE OR REPLACE FUNCTION precis_sha_float(p double precision)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT quote_nullable(encode(float8send(p), 'hex')) $$;

-- An explicit, ordered list of the frozen columns. Text as-is, floats as their
-- IEEE bits, jsonb in its canonical text, bigint/boolean/date in fixed formats;
-- no timestamptz and no meta/annotation columns, so it does not move with the
-- session's TimeZone, DateStyle or extra_float_digits. Setting trusted /
-- superseded_* / extraction_status never stales a review; a frozen field cannot
-- change, except that losing the source ref or the anchoring link (ON DELETE
-- SET NULL) correctly stales it.
CREATE OR REPLACE FUNCTION precis_measure_sha(p_id bigint)
    RETURNS text LANGUAGE sql STABLE AS $$
    SELECT left(md5(concat_ws('|',
        precis_sha_part(m.subject_ref_id::text),
        precis_sha_part(m.measurand_ref_id::text),
        precis_sha_part(m.literal),
        precis_sha_part(m.reported_unit),
        precis_sha_float(m.value_num),
        precis_sha_float(m.value_low),
        precis_sha_float(m.value_high),
        precis_sha_part(m.value_text),
        precis_sha_part((CASE m.value_bool WHEN true THEN 't' WHEN false THEN 'f' END)::text),
        precis_sha_float(m.value_err),
        precis_sha_part(m.value_form),
        precis_sha_part(m.reference),
        precis_sha_part(m.tier),
        precis_sha_part(m.normalization),
        precis_sha_part(m.normalization_status),
        precis_sha_part(m.source_attribution),
        precis_sha_part(m.measurand_status),
        precis_sha_part(m.direction),
        precis_sha_part(m.role),
        precis_sha_part(m.run_key),
        precis_sha_part(m.subject),
        precis_sha_part(m.subject_group),
        precis_sha_part(m.experiment_ref_id::text),
        precis_sha_part(m.derived_from::text),
        precis_sha_part(m.primary_link_id::text),
        precis_sha_part(m.anchor_scheme),
        precis_sha_part(m.span::text),
        precis_sha_part(m.supersedes::text),
        precis_sha_part(m.source_ref_id::text),
        precis_sha_part(m.actor),
        precis_sha_part(m.model),
        precis_sha_part(m.input_unit),
        precis_sha_part(m.conditions::text),
        precis_sha_part(m.maturity),
        precis_sha_part(m.method),
        precis_sha_part(m.source_chunk),
        precis_sha_part(m.source_url),
        precis_sha_part(to_char(m.as_of, 'YYYY-MM-DD')),
        precis_sha_part(m.notes))), 16)
      FROM measures m WHERE m.id = p_id
$$;

CREATE OR REPLACE FUNCTION precis_target_sha(p_kind text, p_id bigint)
    RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
    s text;
BEGIN
    IF p_kind = 'chunk' THEN
        SELECT coalesce(c.content_sha, 'chunk:' || c.chunk_id) INTO s
          FROM chunks c WHERE c.chunk_id = p_id;
    ELSIF p_kind = 'ref' THEN
        SELECT precis_ref_sha_of(r, precis_body_sha(r.ref_id)) INTO s
          FROM refs r WHERE r.ref_id = p_id;
    ELSIF p_kind = 'link' THEN
        SELECT precis_link_sha_of(l) INTO s FROM links l WHERE l.link_id = p_id;
    ELSIF p_kind = 'measure' THEN
        s := precis_measure_sha(p_id);
    END IF;
    RETURN s;
END
$$;

-- ── 9. the old names return as views ───────────────────────────────────
-- Everything above scanned or wrote; from here on, statements take locks on
-- refs / links that are held to COMMIT, so nothing below may scan a large table.

-- The rows now live in measures (count asserted in section 5). Dropping a table
-- that references refs locks refs, hence its place here.
-- squawk-ignore ban-drop-table
DROP TABLE component_spec_values;

CREATE VIEW material_values AS
SELECT m.id, m.subject_ref_id AS material_ref_id,
       t.meta -> 'legacy_source' ->> 'key' AS property_id,
       m.value_num, m.value_low, m.value_high, m.value_text, m.value_bool,
       m.input_unit, m.conditions, m.maturity, m.method,
       m.source_ref_id, m.source_chunk, m.source_url, m.as_of,
       CASE WHEN m.actor = 'legacy' THEN NULL ELSE m.actor END AS set_by,
       m.created_at, m.notes
  FROM measures m
  JOIN refs t ON t.ref_id = m.measurand_ref_id
 WHERE m.superseded_by IS NULL
   AND t.meta -> 'legacy_source' ->> 'table' = 'material_properties';

CREATE VIEW component_spec_values AS
SELECT m.id, m.subject_ref_id AS component_ref_id,
       t.meta -> 'legacy_source' ->> 'key' AS spec_id,
       m.value_num, m.value_low, m.value_high, m.value_text, m.value_bool,
       m.input_unit, m.conditions, m.maturity, m.method,
       m.source_ref_id, m.source_chunk, m.source_url, m.as_of,
       CASE WHEN m.actor = 'legacy' THEN NULL ELSE m.actor END AS set_by,
       m.created_at, m.notes
  FROM measures m
  JOIN refs t ON t.ref_id = m.measurand_ref_id
 WHERE m.superseded_by IS NULL
   AND t.meta -> 'legacy_source' ->> 'table' = 'component_specs';

COMMENT ON VIEW material_values IS
    'Compatibility view over measures (live rows whose measurand came from material_properties). INSERT only, through an INSTEAD OF trigger that synthesises literal and run_key; there is no UPDATE or DELETE door.';
COMMENT ON VIEW component_spec_values IS
    'Compatibility view over measures (live rows whose measurand came from component_specs). INSERT only, through an INSTEAD OF trigger that synthesises literal and run_key; there is no UPDATE or DELETE door.';

CREATE OR REPLACE FUNCTION precis_legacy_value_insert()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_table  text := TG_ARGV[0];
    v_key    text;
    v_tax    bigint;
    v_id     bigint;
    v_subj   bigint;
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
    v_id := nextval(pg_get_serial_sequence('measures', 'id'));
    INSERT INTO measures
        (id, subject_ref_id, measurand_ref_id, value_num, value_low, value_high,
         value_text, value_bool, input_unit, conditions, maturity, method,
         source_ref_id, source_chunk, source_url, as_of, actor, notes,
         literal, value_form, run_key)
    OVERRIDING SYSTEM VALUE
    VALUES
        (v_id, v_subj, v_tax, NEW.value_num, NEW.value_low, NEW.value_high,
         NEW.value_text, NEW.value_bool, NEW.input_unit,
         coalesce(NEW.conditions, '{}'::jsonb), coalesce(NEW.maturity, 'lab'),
         NEW.method, NEW.source_ref_id, NEW.source_chunk, NEW.source_url,
         NEW.as_of, coalesce(nullif(btrim(NEW.set_by), ''), 'legacy'), NEW.notes,
         precis_measure_literal(NEW.value_num, NEW.value_low, NEW.value_high,
                                NEW.value_text, NEW.value_bool),
         precis_measure_form(NEW.value_num, NEW.value_low, NEW.value_high,
                             NEW.value_text, NEW.value_bool),
         'legacy:' || v_id);
    NEW.id := v_id;
    NEW.created_at := now();
    RETURN NEW;
END
$$;

CREATE TRIGGER material_values_insert INSTEAD OF INSERT ON material_values
    FOR EACH ROW EXECUTE FUNCTION precis_legacy_value_insert('material_properties');
CREATE TRIGGER component_spec_values_insert INSTEAD OF INSERT ON component_spec_values
    FOR EACH ROW EXECUTE FUNCTION precis_legacy_value_insert('component_specs');

-- Foreign keys last: each ADD takes a lock on its referenced table. The subject
-- FK is inherited as ON DELETE CASCADE; it becomes RESTRICT. The primary-link
-- and source FKs SET NULL (the frozen trigger lets exactly that through).
ALTER TABLE measures DROP CONSTRAINT material_values_material_ref_id_fkey;
ALTER TABLE measures
    ADD CONSTRAINT measures_subject_fk FOREIGN KEY (subject_ref_id)
        REFERENCES refs (ref_id) ON DELETE RESTRICT NOT VALID,
    ADD CONSTRAINT measures_measurand_fk FOREIGN KEY (measurand_ref_id)
        REFERENCES refs (ref_id) NOT VALID,
    ADD CONSTRAINT measures_experiment_fk FOREIGN KEY (experiment_ref_id)
        REFERENCES refs (ref_id) NOT VALID,
    ADD CONSTRAINT measures_primary_link_fk FOREIGN KEY (primary_link_id)
        REFERENCES links (link_id) ON DELETE SET NULL NOT VALID,
    ADD CONSTRAINT measures_supersedes_fk FOREIGN KEY (supersedes)
        REFERENCES measures (id) NOT VALID,
    ADD CONSTRAINT measures_superseded_by_fk FOREIGN KEY (superseded_by)
        REFERENCES measures (id) NOT VALID;
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE measures VALIDATE CONSTRAINT measures_subject_fk;
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE measures VALIDATE CONSTRAINT measures_measurand_fk;
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE measures VALIDATE CONSTRAINT measures_experiment_fk;
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE measures VALIDATE CONSTRAINT measures_primary_link_fk;
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE measures VALIDATE CONSTRAINT measures_supersedes_fk;
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE measures VALIDATE CONSTRAINT measures_superseded_by_fk;

DROP TRIGGER IF EXISTS refs_taxon_unit_guard ON refs;
CREATE TRIGGER refs_taxon_unit_guard
    BEFORE UPDATE OF meta ON refs FOR EACH ROW
    WHEN (NEW.kind = 'taxon'
          AND (OLD.meta -> 'canonical_unit' IS DISTINCT FROM NEW.meta -> 'canonical_unit'
               OR OLD.meta -> 'dimension_kind' IS DISTINCT FROM NEW.meta -> 'dimension_kind'
               OR OLD.meta -> 'si_vector' IS DISTINCT FROM NEW.meta -> 'si_vector'))
    EXECUTE FUNCTION precis_taxon_unit_guard();

-- A taxon's required_conditions are content (they decide what gets flagged).
-- Last: 0185's kinds trigger re-takes locks on refs and links. Taxon is already
-- covered, so the refresh finds the same WHEN lists and runs no DDL (the trigger
-- oids are asserted unchanged in tests/test_measures.py, not here).
UPDATE kinds SET covered_meta = covered_meta || ARRAY['required_conditions']
 WHERE slug = 'taxon' AND covered_meta IS NOT NULL
   AND NOT ('required_conditions' = ANY (covered_meta));

COMMIT;

-- End of 0187_measures.sql
