-- 0023_se_bound_kind_pcb.sql
--
-- pcb→se binding, rough box (docs/backlog/pcb-se-binding.md §Rough box
-- v0): an se block may bind a ``pcb`` design as its L3 realization, so
-- ``se_blocks_bound_kind_check`` widens with ``'pcb'``. The binding is
-- slug-keyed and resolved at read time by
-- ``precis_se.persist.attach_boards`` — exactly the ``component``
-- posture: no live-row FK, no resolution gate at persist.
--
-- Re-stated NOT VALID like 0008's version — the constraint governs new
-- writes; no live row is re-checked (and none needs to be: the widening
-- only admits a value the old CHECK rejected, never tightens).
--
-- Forward-only (ADR 0005). Idempotent. PLUGIN migration (namespace
-- ``precis_se``).

BEGIN;

ALTER TABLE se_blocks DROP CONSTRAINT IF EXISTS se_blocks_bound_kind_check;
ALTER TABLE se_blocks ADD CONSTRAINT se_blocks_bound_kind_check
    CHECK (bound_kind IN ('cad', 'structure', 'component', 'part', 'pcb'))
    NOT VALID;

COMMIT;

-- End of 0023_se_bound_kind_pcb.sql
