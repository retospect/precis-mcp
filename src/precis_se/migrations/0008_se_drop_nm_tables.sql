-- precis_se/0008_se_drop_nm_tables.sql
--
-- The nm→se merge, last step (docs/backlog/nm-se-merge.md "Retire the `nm`
-- kind" + "Migrations"): the ``nm`` kind is gone from ``pyproject.toml``'s
-- entry points (handler, job type, migration namespace, handle code), its
-- domain layer lives in :mod:`precis_se.atomic`, and its storage is dead.
-- Drop it.
--
-- Why this lives in the ``precis_se`` namespace and not ``precis_nm``:
-- ``precis_nm``'s migration namespace is sealed history and its package no
-- longer exists, so nothing would ever run a 0008 there. The se plugin is
-- the *successor* of that storage — se already carries every column the nm
-- tables held (``0007_se_atomic.sql``) — so the forward-only step that
-- retires the predecessor belongs to it (ADR 0005).
--
-- ORDERING (operator-visible, and the reason this is its own migration
-- rather than part of 0007): prod's ``nm`` designs are deleted
-- user-runs-it BEFORE this deploys — see
-- ``docs/runbooks/retire-nm-kind-prod.md``. All three prod nm designs were
-- already soft-retired when this was written; the drop below is
-- unconditional either way, because a table whose kind has no handler can
-- no longer be read by anything.
--
-- CASCADE, not a hand-ordered drop list: ``nm_ports`` FK-references
-- ``nm_blocks`` and ``nm_topology``/``nm_connects`` reference ``refs``, and
-- every index/constraint on the four tables goes with them. Nothing outside
-- this set depends on them (``se_*`` never referenced an ``nm_*`` table —
-- the merge moved content through the handler layer, not through SQL).
--
-- ``se_blocks.bound_kind`` also loses ``'nm'``: an L3 realization can no
-- longer point at a design kind that does not exist
-- (:data:`precis_se.ops._BINDING_KINDS`, :data:`precis_se.modes.
-- ATOMIC_ONLY_BINDING_KINDS`). Re-stated NOT VALID like 0007's version —
-- the constraint governs new writes; no live se row carries ``'nm'``
-- (prod's se tables had zero rows as of 0007, and nothing has written an
-- nm binding since).
--
-- Forward-only (ADR 0005). Idempotent. PLUGIN migration (namespace
-- ``precis_se``), applied after 0001-0007.

BEGIN;

DROP TABLE IF EXISTS nm_ports CASCADE;
DROP TABLE IF EXISTS nm_connects CASCADE;
DROP TABLE IF EXISTS nm_topology CASCADE;
DROP TABLE IF EXISTS nm_blocks CASCADE;

ALTER TABLE se_blocks DROP CONSTRAINT IF EXISTS se_blocks_bound_kind_check;
ALTER TABLE se_blocks ADD CONSTRAINT se_blocks_bound_kind_check
    CHECK (bound_kind IN ('cad', 'structure', 'component', 'part'))
    NOT VALID;

COMMIT;

-- End of 0008_se_drop_nm_tables.sql
