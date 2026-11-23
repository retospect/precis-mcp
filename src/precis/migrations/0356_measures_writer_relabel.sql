-- 0356_measures_writer_relabel.sql
--
-- The writer-relabel door (gr727770). The AIxMAT 2026 reading waves of
-- 2026-11-18 ran gpt-6.2-astra at reasoning effort xhigh, but the reading
-- brief hard-coded the stale id in args={'writer': {'model': 'gpt-6-astra'}}
-- (~/.codex/config.toml had changed after the pilot): about 1047 measure
-- rows carry model='gpt-6-astra' although gpt-6.2-astra wrote them. The
-- MCP doors cannot correct it (a measure row's meta is frozen; meta.model
-- is rejected on finding edits, gr715492 / gr715597), so this is a one-off
-- database update scoped by model, created_at and operator-verified IDs.
--
-- `measures` is append-only (0187): `precis_measures_frozen` refuses every
-- UPDATE but the annotations, a same-measurand supersession and the 0234
-- re-point, so a wrong writer model could never be corrected — and the
-- writer field is expected to name the model that wrote the row. This
-- migration keeps the rule and opens one narrow door beside the others:
-- inside a transaction that set `precis.allow_writer_relabel = 'on'`
-- (transaction-local — pgbouncer-safe, same shape as 0234's flag),
-- `model` — and only `model` — may change, and only from 'gpt-6-astra'
-- to 'gpt-6.2-astra' (the exact mislabel gr727770 names). The pair is
-- pinned in the trigger, not the caller: under the flag any other old or
-- new value, or any other column moving with it, is refused, so the door
-- applies only to rows created on 2026-11-18 UTC. Every other column stays frozen.
--
-- Caveat the door keeps honest by naming it: `precis_measure_sha` covers
-- `model`, so a relabel stales any review pinned on the row (same caveat
-- 0234 carries for measurand_ref_id). The one-off runner
-- (scripts/writer-relabel) checks for live pinned reviews and refuses the
-- run while any exist. The runner locks the ledger before the check.
--
-- Rollback shape: a forward migration restoring this migration's
-- pre-change body of precis_measures_frozen() (drop the v_relabel_ok
-- clause and its filter term). Forward-only (ADR 0005). One transaction.

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
    -- the writer-relabel door (0356, gr727770): under the transaction-local
    -- flag, model — nothing else — may move off the stale writer id the
    -- AIxMAT 2026-11-18 reading waves were stamped with onto the model that
    -- actually ran. The pair is pinned here, so the flag cannot relabel
    -- anything else or move any other column.
    v_relabel_ok boolean :=
        coalesce(current_setting('precis.allow_writer_relabel', true), '')
            = 'on'
        AND OLD.model IS DISTINCT FROM NEW.model
        AND OLD.created_at >= TIMESTAMPTZ '2026-11-18 00:00:00+00'
        AND OLD.created_at < TIMESTAMPTZ '2026-11-19 00:00:00+00'
        AND OLD.model = 'gpt-6-astra'
        AND NEW.model = 'gpt-6.2-astra'
        AND to_jsonb(NEW) - 'model' IS NOT DISTINCT FROM to_jsonb(OLD) - 'model';
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
           AND NOT (coalesce(n.key, o.key) = 'model' AND v_relabel_ok IS TRUE)
    LOOP
        RAISE EXCEPTION
            'measures are append-only: column % of measure % is frozen '
            '(only trusted, extraction_status -> human_checked, a first '
            'superseded_by + superseded_at onto a row with the same subject, '
            'measurand and direction, a primary_link_id / source_ref_id '
            'whose target is gone, a measurand_ref_id re-point under '
            'precis.allow_measurand_repoint onto a live taxon with the same '
            'unit contract, and a writer re-label of model from '
            'gpt-6-astra to gpt-6.2-astra under precis.allow_writer_relabel '
            'may change; supersede with a new row)',
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

-- End of 0356_measures_writer_relabel.sql
