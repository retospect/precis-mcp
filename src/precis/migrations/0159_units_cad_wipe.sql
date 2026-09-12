-- 0159_units_cad_wipe.sql
--
-- units-policy-cutover (docs/backlog/units-policy-cutover.md): the cad
-- DSL/scene ingest boundary now stores canonical lengths in SI metres
-- (chain round 4, `precis.cad.dsl` / `precis.cad.scene`) instead of the
-- pre-cutover implicit-millimetre convention. Every cad design that
-- predates the cutover is dev/test data only — no live production
-- design needs preserving through the unit change (Reto, 2026-09-12) —
-- so this is a clean-slate wipe, not a numeric rewrite: retire every
-- kind='cad' ref (the house soft-delete convention — mirrors
-- `precis.store._cad_ops.CadMixin.cad_delete`) and drop its cad_nodes
-- rows + card_combined search chunk. A `put` after this migration
-- authors fresh, unit-suffixed source through the new strict boundary
-- from scratch.
--
-- cad_nodes rows are hard-deleted rather than retired: they carry no
-- independent value once their owning ref is retired (cad_load/probe/
-- relate all filter to live refs first, so a retired design's geometry
-- is never read again), and `cad_nodes.ref_id` already cascades on a
-- `refs` hard-delete — deleting them explicitly here just makes the
-- wipe's intent (nothing left to numerically migrate) visible in one
-- place instead of relying on a future hard-delete to clean up.
--
-- Forward-only (ADR 0005). Idempotent: retiring an already-retired ref
-- and deleting rows that are already gone are both no-ops.

BEGIN;

DELETE FROM cad_nodes
WHERE ref_id IN (SELECT ref_id FROM refs WHERE kind = 'cad');

DELETE FROM chunks
WHERE chunk_kind = 'card_combined'
  AND ref_id IN (SELECT ref_id FROM refs WHERE kind = 'cad');

UPDATE refs
SET retired_at = now()
WHERE kind = 'cad' AND retired_at IS NULL;

COMMIT;

-- End of 0159_units_cad_wipe.sql
