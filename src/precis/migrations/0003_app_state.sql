-- ===========================================================================
-- 0003_app_state.sql — small key/value table for boot-time state.
--
-- v2 dropped the dedicated `system` key/value table. The only in-tree
-- consumer was `precis.jobs.oracle_sync`, which uses it to cache the
-- oracle YAML version across boots so the bundled corpus is only
-- re-embedded when its sha256 actually changes. Without persistent
-- storage, `_read_state` always returned None and every boot re-embedded
-- ~6000 oracle YAML files (~30-60s of wasted work per `precis` invocation,
-- on every container restart, on every CLI call).
--
-- This migration adds `app_state` as the v2-native replacement. Scope is
-- intentionally narrow: a tiny upsert/select surface for cross-boot
-- bookkeeping rows that don't belong on a ref. Keys today (`corpus.oracle.*`)
-- live in `precis.jobs.oracle_sync`; future keys are free to add without
-- a migration.
-- ===========================================================================


CREATE TABLE app_state (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE app_state IS
    'Process-global key/value rows for cross-boot bookkeeping (e.g. oracle '
    'YAML version cache). Not a generic kv-store — confine usage to setup '
    'state that legitimately has no home on a ref.';
