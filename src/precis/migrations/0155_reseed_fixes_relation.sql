-- 0155_reseed_fixes_relation.sql
--
-- Re-affirm the ``fixes`` / ``fixed-by`` relation pair originally seeded
-- by 0006_fix_gripe_relation.sql (gr250037).
--
-- gr250037: `link(kind='todo', id=N, target='gripe:M', rel='fixes')` —
-- the exact call `precis-fix-gripe-help` documents as the Slice-5
-- canonical pattern — raised a raw `[error:Internal] ... ForeignKeyViolation`
-- on some deployments, while the identical call with `rel=` omitted
-- (default `related-to`) succeeded. Root cause: `'fixes'` lives in the
-- static `Relation` Literal (`store/types.py`), so
-- `validate_relation`'s handler-layer pre-flight accepts it via the
-- fast built-in-literal path *without ever consulting the live
-- ``relations`` table* — that live-DB check only runs for relations
-- the literal doesn't already know. A deployment whose ``relations``
-- table is missing the `fixes`/`fixed-by` rows (0006 never having
-- taken effect there, whatever the history) sails past the app-layer
-- guard and hits Postgres' FK on ``links.relation`` raw, which nothing
-- downstream narrowly caught (see the accompanying hardening fix in
-- ``store/_links_ops.py``).
--
-- This migration doesn't change *why* a deployment's vocabulary could
-- be missing the pair — it just heals it forward, the same
-- `ON CONFLICT DO NOTHING` idempotent-seed pattern 0006 already used
-- (so a correctly-migrated DB, where the row already exists, no-ops).
--
-- Forward-only (ADR 0005).

BEGIN;

INSERT INTO relations (slug, inverse_slug, description) VALUES
    ('fixes',    'fixed-by',
     'Source ref offers a fix for the target ref (e.g. a fix_gripe job → its gripe)'),
    ('fixed-by', 'fixes',
     'Source ref is being fixed by the target ref')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0155_reseed_fixes_relation.sql
