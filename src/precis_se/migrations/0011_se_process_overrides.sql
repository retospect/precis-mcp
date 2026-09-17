-- precis_se/0011_se_process_overrides.sql
--
-- The override home docs/backlog/se-print-implementer.md rung 1 (se-
-- kind.md L5's "One resolver" bullet) needed: a block-owned facet that
-- lets a design tighten or loosen a manufacturing-capability figure per
-- block, never below the process's physical floor. Written/cleared by
-- :mod:`precis_se.ops`'s `set_process_override`/`clear_process_override`,
-- read through the resolver chain in `precis_se.capabilities.resolve`
-- (block override -> load-derived, unimplemented -> `capability()`'s
-- house tier, clamped to the physical figure).
--
-- `se_blocks.process_overrides` — one JSON object keyed by capability
-- field name (`{'max_overhang': 35}`), block-owned like `dof`/
-- `objectives`/`chromophore` rather than a table of its own: one per
-- block, meaningless without it, gone when it goes. `NULL` = no
-- overrides. A field this block's mode family does not define, or a
-- value beyond the field's physical floor, is rejected at WRITE time
-- (`set_process_override`) — never stored malformed; `resolve()` clamps
-- defensively on read regardless, for the tree a caller builds by hand
-- rather than through the ops layer.
--
-- Zero live rows as of this migration (no design has ever had a facet to
-- store here).
--
-- The migration bodies get `BEGIN;`/`COMMIT;` stripped and executed
-- whole by the plugin-migration test fixtures (0009's header), so every
-- statement below stays independently runnable.
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration
-- (namespace `precis_se`), applied after 0001-0010.

BEGIN;

ALTER TABLE se_blocks ADD COLUMN IF NOT EXISTS process_overrides jsonb;

COMMENT ON COLUMN se_blocks.process_overrides IS
    'Per-block overrides of a manufacturing-capability figure '
    '(precis_se.capabilities.resolve''s override tier), keyed by '
    'capability field name: {"max_overhang": 35}. Block-owned like dof/'
    'objectives/chromophore, not a table of its own. NULL = no overrides. '
    'Written by ops set_process_override/clear_process_override, which '
    'reject a field the block''s mode family does not define and a value '
    'beyond the field''s physical floor at write time.';

COMMIT;

-- End of 0011_se_process_overrides.sql
