-- 0268_gripe_status_verify_split.sql
--
-- Gripe status ladder (gr543584, gr541800, gr519676): split ``verify``
-- in two — ``auto_verify`` means code can check the fix (the deploy-time
-- validator closes it once the sha is in prod), ``user_verify`` means a
-- human has to check it (the validator files one ask-user todo per
-- deploy and the gripes close when it is answered). The ladder is
-- open → triaged → fixing → auto_verify|user_verify → done | wontfix.
-- ``verify`` stays accepted as the legacy spelling of ``auto_verify``
-- (rows on prod were written under 0247 semantics) — the validator
-- treats the pair as one cohort. ``ready_for_fix`` / ``in_review``
-- stay accepted (legacy rows).
--
-- Plus ``needs_spec`` (gr543698): triage parks an ill-specified gripe
-- there (a spec must be written before a builder picks it); a
-- docs/backlog spec naming it, or a human's "back to new" (STATUS:open),
-- moves it on.
--
-- Same shape as 0267: the ship gate bans DROP FUNCTION, so this CREATE
-- OR REPLACEs ``gripe_status_check()`` with the eleven-value list; the
-- three 0176 constraint triggers call it by name and are untouched.
-- Existing rows are all in-vocabulary already, so no backfill.
-- Forward-only (ADR 0005).

BEGIN;

INSERT INTO tags (namespace, value) VALUES
    ('STATUS', 'auto_verify'),
    ('STATUS', 'user_verify'),
    ('STATUS', 'needs_spec')
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
                'done', 'wontfix'))
      INTO v_n, v_bad
      FROM ref_tags rt
      JOIN tags t ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
     WHERE rt.ref_id = v_ref_id;

    IF v_n <> 1 OR v_bad > 0 THEN
        RAISE EXCEPTION
            'gripe % must have exactly one STATUS tag from '
            '(open, triaged, fixing, ready_for_fix, in_review, verify, '
            'auto_verify, user_verify, needs_spec, done, wontfix); '
            'found % STATUS tag(s), % off-vocabulary',
            v_ref_id, v_n, v_bad
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;

COMMIT;
