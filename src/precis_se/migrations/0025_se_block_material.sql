-- 0025_se_block_material.sql
--
-- The block's explicit material (gr514245's remaining manifest slice —
-- docs/backlog/se-native-fields.md ruling Q3): ``se_blocks.material`` —
-- a ``kind='material'`` ref slug, block-owned like ``mode``. Written by
-- the ``set_material`` op (the workspace importer's block ``material``
-- manifest key replays it); NULL = undeclared — readers fall back to
-- ``set_mode``'s ``family/material`` half, and a slug that resolves to
-- no row is a named not-applicable at read, never a write-time
-- rejection (slug-keyed like ``bound_design``: no live-row FK).
--
-- The migration bodies get ``BEGIN;``/``COMMIT;`` stripped and executed
-- whole by the plugin-migration test fixtures (0009's header), so every
-- statement below stays independently runnable.
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration
-- (namespace ``precis_se``).

BEGIN;

ALTER TABLE se_blocks ADD COLUMN IF NOT EXISTS material text;

COMMENT ON COLUMN se_blocks.material IS
    'The block''s explicit material — a kind=''material'' ref slug '
    '(precis_se.ops set_material; the workspace manifest''s block-level '
    'material key replays it). NULL = undeclared: readers fall back to '
    'mode''s family/material half. Slug-keyed — resolved at read; a '
    'missing row is a named not-applicable, never a write rejection.';

COMMIT;

-- End of 0025_se_block_material.sql
