-- 0269_gripe_status_parked.sql
--
-- Gripe ``STATUS:parked`` (gr543711): a parked gripe waits on a wake
-- condition stored in ``refs.meta->'until'`` (``{type, value, note}``).
-- The periodic ``gripe_parked`` cadence evaluates it and sends the gripe
-- back to ``open`` when the condition is met; until then it is off the
-- open queue — ``/open``, the live cohort and the open-count lines all
-- exclude it, so a waiting gripe costs no triage attention.
--
-- Same shape as 0268: the ship gate bans DROP FUNCTION, so this CREATE
-- OR REPLACEs ``gripe_status_check()`` with the twelve-value list; the
-- three 0176 constraint triggers call it by name and are untouched.
-- Existing rows are all in-vocabulary already, so no backfill.
-- Forward-only (ADR 0005).

BEGIN;

INSERT INTO tags (namespace, value) VALUES ('STATUS', 'parked')
    ON CONFLICT (namespace, value) DO NOTHING;

CREATE OR REPLACE FUNCTION public.gripe_status_check() RETURNS trigger
    LANGUAGE plpgsql AS $$
DECLARE
    v_ref_id bigint;
    v_n      integer;
    v_bad    integer;
BEGIN
    IF TG_TABLE_NAME = 'refs' THEN
        v_ref_id := NEW.ref_id;
    ELSIF TG_OP = 'DELETE' THEN
        v_ref_id := OLD.ref_id;
    ELSE
        v_ref_id := NEW.ref_id;
    END IF;

    -- Only gripes that still exist (a cascade delete removes the ref
    -- before this deferred trigger runs).
    IF NOT EXISTS (SELECT 1 FROM refs WHERE ref_id = v_ref_id AND kind = 'gripe') THEN
        RETURN NULL;
    END IF;

    SELECT count(*),
           count(*) FILTER (WHERE t.value NOT IN
               ('open', 'triaged', 'fixing', 'ready_for_fix', 'in_review',
                'verify', 'auto_verify', 'user_verify', 'needs_spec',
                'parked', 'done', 'wontfix'))
      INTO v_n, v_bad
      FROM ref_tags rt
      JOIN tags t ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
     WHERE rt.ref_id = v_ref_id;

    IF v_n <> 1 OR v_bad > 0 THEN
        RAISE EXCEPTION
            'gripe % must have exactly one STATUS tag from '
            '(open, triaged, fixing, ready_for_fix, in_review, verify, '
            'auto_verify, user_verify, needs_spec, parked, done, wontfix); '
            'found % STATUS tag(s), % off-vocabulary',
            v_ref_id, v_n, v_bad
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;

COMMIT;
