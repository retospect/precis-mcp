-- 0338_measures_escalation_repair.sql
--
-- gr722340: the implausible_value door compares a reading against the
-- measurand's live median because that is what a stripped unit prefix
-- looks like ('6 µm' extracted as '6 m', gr228594). A reading that
-- states no unit — reported_unit NULL, blank, or the bare '1' the
-- extractor writes for a unitless cell on a canonical-'1' measurand —
-- has no prefix to lose, so its spread is real: mx6128, the SPICE
-- conformation total of 1132808 on the unitless Count measurand
-- tn465113, was flagged all the same, and `best_measure` reads any
-- `meta ? 'escalation'` row as excluded.
--
-- The write path is fixed in the same change (a unitless reading now
-- skips the check); this migration does the other half — repairing the
-- rows the old check already flagged. `meta` is frozen like every other
-- measures column, so it opens one narrow door beside the 0234 one:
-- inside a transaction that set
-- `precis.allow_measure_escalation_repair = 'on'` (transaction-local,
-- pgbouncer-safe), `meta` — and only `meta` — may change, and only by
-- shrinking an escalation array that is already there:
--
--   * `jsonb_typeof(OLD.meta->'escalation') = 'array'` — the door cannot
--     grow an `escalation` key on a clean row (a bare '[]' would still
--     trip `meta ? 'escalation'` and exclude the row, gr722340 review);
--   * every other meta key is IS NOT DISTINCT FROM preserved;
--   * the new array only ever loses entries — every element must be one
--     the old array held, with its original multiplicity as an upper bound,
--     and the array must be shorter. When the last
--     entry goes the key must be removed, never left as '[]'.
--
-- The repair itself runs here, in this transaction, scoped to exactly
-- the rows the new check would no longer flag: live output rows whose
-- reported_unit is unitless carrying an implausible_value entry. Other
-- rules on the same row stay; superseded rows keep their flags (dead
-- rows rank nowhere). precis_measure_sha does not cover meta, so the
-- repair stales no review.
--
-- Rollback shape: a forward migration restoring the 0234 body of
-- precis_measures_frozen() (drop the v_esc_ok clause and its filter
-- term). Repaired rows are not restorable — the flag was wrong
-- (forward-only, ADR 0005). One transaction.
--
BEGIN;

-- The runner sets lock_timeout = 0 for the apply (store/migrate.py); bound
-- the catalog write so it fails fast behind a long transaction instead of
-- queueing.
SET LOCAL lock_timeout = '5s';

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
    -- the measurand re-point door (0234): under the transaction-local flag,
    -- measurand_ref_id — nothing else — may move onto a live taxon with the
    -- same dimension contract (canonical_unit, dimension_kind, si_vector all
    -- IS NOT DISTINCT FROM equal). The stored numbers keep their unit.
    v_repoint_ok boolean :=
        coalesce(current_setting('precis.allow_measurand_repoint', true), '')
            = 'on'
        AND OLD.measurand_ref_id IS DISTINCT FROM NEW.measurand_ref_id
        AND EXISTS (SELECT 1
                      FROM refs s
                      JOIN refs d ON d.ref_id = NEW.measurand_ref_id
                     WHERE s.ref_id = OLD.measurand_ref_id
                       AND s.kind = 'taxon' AND d.kind = 'taxon'
                       AND d.retired_at IS NULL
                       AND s.meta -> 'canonical_unit'
                           IS NOT DISTINCT FROM d.meta -> 'canonical_unit'
                       AND s.meta -> 'dimension_kind'
                           IS NOT DISTINCT FROM d.meta -> 'dimension_kind'
                       AND s.meta -> 'si_vector'
                           IS NOT DISTINCT FROM d.meta -> 'si_vector');
    -- the escalation-repair door (0338): under the transaction-local flag,
    -- meta may change only by shrinking an escalation array that is already
    -- there — an entry may drop, never be added, lengthened or rewritten,
    -- and the last one going drops the key (a leftover '[]' still trips
    -- `meta ? 'escalation'`). A clean row cannot grow the key at all.
    v_esc_ok boolean :=
        coalesce(current_setting('precis.allow_measure_escalation_repair',
                                 true), '')
            = 'on'
        AND (OLD.meta - 'escalation')
            IS NOT DISTINCT FROM (NEW.meta - 'escalation')
        AND CASE
            WHEN jsonb_typeof(OLD.meta -> 'escalation') IS DISTINCT FROM 'array'
                THEN false
            WHEN NEW.meta -> 'escalation' IS NULL THEN true
            WHEN jsonb_typeof(NEW.meta -> 'escalation') = 'array' THEN
                jsonb_array_length(NEW.meta -> 'escalation') > 0
                AND jsonb_array_length(NEW.meta -> 'escalation')
                    < jsonb_array_length(OLD.meta -> 'escalation')
                -- EXCEPT ALL compares whole entries and consumes one old
                -- occurrence for each occurrence in the new array.
                AND NOT EXISTS (
                    SELECT value FROM jsonb_array_elements(NEW.meta -> 'escalation')
                    EXCEPT ALL
                    SELECT value FROM jsonb_array_elements(OLD.meta -> 'escalation')
                )
            ELSE false
        END;
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
           AND NOT (coalesce(n.key, o.key) = 'measurand_ref_id' AND v_repoint_ok)
           AND NOT (coalesce(n.key, o.key) = 'meta' AND v_esc_ok IS TRUE)
    LOOP
        RAISE EXCEPTION
            'measures are append-only: column % of measure % is frozen '
            '(only trusted, extraction_status -> human_checked, a first '
            'superseded_by + superseded_at onto a row with the same subject, '
            'measurand and direction, a primary_link_id / source_ref_id '
            'whose target is gone, a measurand_ref_id re-point under '
            'precis.allow_measurand_repoint onto a live taxon with the same '
            'unit contract, and a shrink-only meta.escalation repair under '
            'precis.allow_measure_escalation_repair may change; supersede '
            'with a new row)',
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

-- The repair itself: drop every implausible_value entry off a live output
-- row whose reading stated no unit — the exact set the write path no
-- longer flags — and drop the escalation key when the last entry goes.
-- Entries whose rule is anything else (or missing) stay verbatim.
SELECT set_config('precis.allow_measure_escalation_repair', 'on', true);

UPDATE measures m
   SET meta = CASE
        WHEN NOT EXISTS (SELECT 1
                           FROM jsonb_array_elements(m.meta -> 'escalation') e
                          WHERE e ->> 'rule'
                                IS DISTINCT FROM 'implausible_value')
        THEN m.meta - 'escalation'
        ELSE jsonb_set(
            m.meta, '{escalation}',
            (SELECT jsonb_agg(e)
               FROM jsonb_array_elements(m.meta -> 'escalation') e
              WHERE e ->> 'rule'
                    IS DISTINCT FROM 'implausible_value'))
       END
 WHERE m.direction = 'output'
   AND m.superseded_by IS NULL
   -- Python str.strip() whitespace, independent of the database locale.
   AND (m.reported_unit IS NULL OR btrim(
       m.reported_unit,
       U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680' ||
       U&'\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A' ||
       U&'\2028\2029\202F\205F\3000'
   ) IN ('', '1'))
   AND CASE WHEN jsonb_typeof(m.meta -> 'escalation') = 'array' THEN
       EXISTS (SELECT 1
                 FROM jsonb_array_elements(m.meta -> 'escalation') e
                WHERE e ->> 'rule' = 'implausible_value')
       ELSE false
   END;

COMMIT;

-- End of 0338_measures_escalation_repair.sql
