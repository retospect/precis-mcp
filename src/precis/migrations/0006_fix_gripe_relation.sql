-- 0006_fix_gripe_relation.sql
--
-- Register the ``fixes`` / ``fixed-by`` relation pair so a
-- ``kind='job'`` row can carry ``link='gripe:42' rel='fixes'``
-- without tripping the foreign-key constraint on
-- ``links.relation``.
--
-- Surfaced during the manual e2e of migration 0005 (the
-- gripe-first-class + jobs rollout) — the planning skill copy and
-- the JobHandler dispatcher both reference ``rel='fixes'`` for
-- the fix_gripe flow, but the vocabulary was never seeded. Adding
-- it here keeps the constraint catalog complete.
--
-- Forward-only (ADR 0005). Idempotent under ``ON CONFLICT DO
-- NOTHING`` so a partial apply can re-run safely.

BEGIN;

INSERT INTO relations (slug, inverse_slug, description) VALUES
    ('fixes',    'fixed-by',
     'Source ref offers a fix for the target ref (e.g. a fix_gripe job → its gripe)'),
    ('fixed-by', 'fixes',
     'Source ref is being fixed by the target ref')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0006_fix_gripe_relation.sql
