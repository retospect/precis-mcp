-- 0234_measures_measurand_repoint.sql
--
-- The measurand re-point door (ontology-v2, Reto's Q8 ruling: duplicate and
-- retired taxa carry live measures a garden merge / retire cannot strand).
--
-- `measures` is append-only: `precis_measures_frozen` refuses every UPDATE
-- but the annotations and a same-measurand supersession, so a measure row
-- could never move measurands — and the garden merge guard
-- (graph/garden/apply.py, store.migrate_links) refuses a loser that still
-- carries measure rows, counting superseded ones too. A duplicate-measurand
-- merge was therefore unappliable the moment the loser held rows.
--
-- This migration keeps the rule and opens one narrow door beside it: inside
-- a transaction that set `precis.allow_measurand_repoint = 'on'`
-- (transaction-local — pgbouncer-safe, same shape as
-- `precis.allow_unit_rebase` in 0188), `measurand_ref_id` — and only that
-- column — may change, and only onto a target that stores values in the
-- same unit context: both endpoints are taxa, the target is live, and
-- canonical_unit, dimension_kind and si_vector are all IS NOT DISTINCT FROM
-- equal (NULL-safe, so a dimensionless -> dimensionless re-point is
-- allowed). The check lives in the trigger, not the caller: even under the
-- flag a re-label onto an incompatible or retired measurand is refused, and
-- every other column stays frozen.
--
-- Why a move, not supersede-per-row: the row's stored numbers are already
-- in the shared SI unit, so nothing about the number changes — the update
-- is a re-label. A supersession chain would also leave the dead original
-- on the loser, and the merge guard counts those rows too. One caveat the
-- door keeps honest by naming it: `precis_measure_sha` covers
-- measurand_ref_id, so a re-point stales any review pinned on the moved
-- row — callers must check reviews first (scripts/onto-repoint does).
--
-- Rollback shape: a forward migration restoring the 0187 body of
-- precis_measures_frozen() (drop the v_repoint_ok clause and its filter
-- term). Forward-only (ADR 0005). One transaction.
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
    LOOP
        RAISE EXCEPTION
            'measures are append-only: column % of measure % is frozen '
            '(only trusted, extraction_status -> human_checked, a first '
            'superseded_by + superseded_at onto a row with the same subject, '
            'measurand and direction, a primary_link_id / source_ref_id '
            'whose target is gone, and a measurand_ref_id re-point under '
            'precis.allow_measurand_repoint onto a live taxon with the same '
            'unit contract may change; supersede with a new row)',
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

COMMIT;

-- End of 0234_measures_measurand_repoint.sql
