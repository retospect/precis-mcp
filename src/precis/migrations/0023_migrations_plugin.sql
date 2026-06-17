-- 0023_migrations_plugin.sql — enable plugin-namespaced migrations.
--
-- Adds a ``plugin`` column to ``_migrations`` so third-party
-- packages (precis-dft and friends) can ship their own forward-only
-- migrations alongside the core ones without colliding on version
-- numbers. The (plugin, version) pair becomes the primary key:
-- precis's ``0001_initial`` and precis_dft's ``0001_dft_kinds`` can
-- coexist without ambiguity.
--
-- Backfill semantics: every row already in ``_migrations`` was
-- written by the precis-core runner, so the ``DEFAULT 'precis'``
-- correctly identifies them. The DEFAULT remains in place so the
-- legacy INSERT shape used by the migration runner during the
-- 0001-through-0022 bootstrap still works on fresh databases —
-- the runner switches to the explicit-plugin INSERT shape only
-- after detecting that this migration has applied. See
-- ``src/precis/store/migrate.py`` for the dispatch.
--
-- See ``docs/design/dft-phase-0-pr-1-plugin-registries.md`` §1.2 for
-- the full motivation; see ``docs/decisions/0005-greenfield-migrations.md``
-- for the forward-only invariant this migration honours.

BEGIN;

ALTER TABLE public._migrations
    ADD COLUMN plugin text NOT NULL DEFAULT 'precis';

-- A row was previously identified by its ``version`` alone. With
-- plugin migrations in scope, two plugins can legitimately ship a
-- ``0001_initial.sql``. Promote the identity to (plugin, version).
-- No primary key existed before this migration; adding one here is
-- the first explicit uniqueness constraint on the ledger.
ALTER TABLE public._migrations
    ADD CONSTRAINT _migrations_pkey PRIMARY KEY (plugin, version);

COMMIT;
