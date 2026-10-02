-- precis_se/0018_se_regions.sql
--
-- The se region property layer, slice A
-- (docs/backlog/se-region-property-layer.md): measures name a taxonomy
-- measurand, and a block carries named pockets.
--
-- * `se_measures.measurand` / `measurand_ref_id` — the measurand a
--   measure was written against, snapshotted at write time by
--   `precis_se.properties.measurand`: the taxon slug (portable — the ops
--   export re-resolves it, and the property-computer registry is keyed by
--   it) and the taxon ref id (identity). The third snapshot, the node's
--   se unit, lands in the EXISTING `unit` column, which therefore now
--   holds any measurand's unit (`C/m^2`, `e`, `''` for a categorical
--   node), not only the closed registry's four. Both NULL = a legacy
--   `unit=`-only measure — every pre-0018 row, byte-identical.
--
-- * `se_pockets` — a pocket is a named set of region selectors plus a
--   shape on one block (`precis_se.pockets`). Shaped exactly like
--   `se_ports`: a row per (block row, name), written in lockstep with the
--   freshly minted block id on every retire-all/reinsert-all save, retired
--   through the block. `regions` is the ordered selector list
--   (`precis_se.datums` grammar); the measures OF a region are not stored
--   here — they are the measures whose `datum` names it, derived at read.
--
-- The migration bodies get `BEGIN;`/`COMMIT;` stripped and executed
-- inside the caller's transaction (precis_se/migrations/__init__.py), so
-- every statement below stays independently runnable.
--
-- Forward-only. Idempotent. A PLUGIN migration (namespace `precis_se`),
-- applied after 0001-0017.

BEGIN;

-- 1. measurand snapshot on measures ---------------------------------------
ALTER TABLE se_measures ADD COLUMN IF NOT EXISTS measurand text;
ALTER TABLE se_measures
    ADD COLUMN IF NOT EXISTS measurand_ref_id bigint
        REFERENCES refs (ref_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS se_measures_measurand_ref_idx
    ON se_measures (measurand_ref_id) WHERE measurand_ref_id IS NOT NULL;

COMMENT ON COLUMN se_measures.measurand IS
    'Measurand taxon slug snapshotted at write (precis_se.properties.'
    'measurand); NULL = a legacy unit=-only measure.';
COMMENT ON COLUMN se_measures.measurand_ref_id IS
    'Measurand taxon ref id (identity); NULL with measurand NULL.';
COMMENT ON COLUMN se_measures.unit IS
    'The measure''s unit: one of m | count | ratio | deg for a legacy '
    'measure, or the measurand''s se unit snapshotted at write ('''' for '
    'a categorical measurand). Relations require agreement.';

-- 2. pockets ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS se_pockets (
    id          bigserial PRIMARY KEY,
    block_id    bigint NOT NULL REFERENCES se_blocks (id) ON DELETE CASCADE,
    name        text NOT NULL,
    shape       text,
    regions     text[] NOT NULL DEFAULT '{}',
    retired_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE se_pockets IS
    'se pockets (precis_se.pockets): a named set of region selectors plus '
    'a shape (cad DSL in the block frame, or ''hull''), one row per (block '
    'row, name), written in lockstep with se_blocks like se_ports.';

CREATE UNIQUE INDEX IF NOT EXISTS se_pockets_block_name_key
    ON se_pockets (block_id, name) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS se_pockets_block_idx
    ON se_pockets (block_id);

COMMIT;
