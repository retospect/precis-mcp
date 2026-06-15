-- 0016_restore_job_kind.sql
--
-- Forward-only restore of the ``'job'`` row in ``kinds``.
--
-- The row was seeded by ``0005_gripe_first_class_and_jobs.sql`` with
-- ``ON CONFLICT (slug) DO NOTHING``. At some point between 2026-06-07
-- (the apply timestamp of 0005) and 2026-06-15, the row was removed
-- from the production ``kinds`` table by hand or by a maintenance
-- script we don't have audit trail for. The symptom: dispatch fails
-- to mint child jobs under any ``LLM:*``-tagged or ``meta.executor``-
-- carrying todo, raising
-- ``BadInput: unknown kind: 'job'`` from
-- :func:`Store._validate_slug_for_kind` because the FK target row
-- is missing.
--
-- This migration is idempotent (``ON CONFLICT (slug) DO NOTHING``);
-- a freshly-migrated DB that already has the row from 0005 just
-- skips the INSERT. Hosts that lost the row pick it back up.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('job', TRUE, 'Job',
     'Offline run of a task — fix this gripe, run a simulation, '
     'benchmark a commit, or one tick of the LLM planner (Slice 5+). '
     'Addressable by numeric id; status via STATUS: tags; comment '
     'timeline via job_event + job_summary chunks. See precis-job-help.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;
