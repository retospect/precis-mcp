-- precis_nm/0007_units_nm_wipe.sql
--
-- units-policy-cutover (docs/backlog/units-policy-cutover.md): the nm
-- handler/ops ingest boundary now stores canonical lengths in SI metres
-- instead of the pre-cutover implicit-Angstrom convention (`nm_blocks
-- .pose_xyz` and every length token in `nm_blocks.envelope`'s cad-DSL
-- string). Every nm design stored before the cutover is dev/test data
-- only — no live production design needs preserving through the unit
-- change (Reto, 2026-09-12, same ruling that landed
-- `0159_units_cad_wipe.sql` on the core `precis` migration source) — so
-- this is a clean-slate wipe, not a numeric rewrite: retire every
-- kind='nm' ref (the house soft-delete convention — mirrors
-- `precis_nm.persist.retire_design`) and drop its block-tree rows
-- (`nm_blocks`/`nm_ports`/`nm_connects`/`nm_topology`) + card_combined
-- search chunk. A fresh `put` after this migration authors a new design
-- through the new strict m-boundary from scratch.
--
-- Child rows are hard-deleted rather than retired: they carry no
-- independent value once their owning ref is retired (`persist.load_tree`
-- always filters to `retired_at IS NULL` rows under a live ref, so a
-- retired design's block tree is never read again), and every one of
-- these tables already cascades on a `refs` hard-delete via its `ref_id`
-- (or, for `nm_ports`, transitively via `nm_blocks.id` — 0001's/0002's
-- `ON DELETE CASCADE`) — deleting them explicitly here just makes the
-- wipe's intent (nothing left to numerically migrate) visible in one
-- place instead of relying on a future hard-delete to clean up.
--
-- Forward-only (ADR 0005). Idempotent: retiring an already-retired ref
-- and deleting rows that are already gone are both no-ops. Plugin
-- migration (namespace `precis_nm`), applied after core via
-- `Migrator.discover_sources`.

BEGIN;

DELETE FROM nm_ports
WHERE block_id IN (
    SELECT nb.id FROM nm_blocks nb
    JOIN refs r ON r.ref_id = nb.ref_id
    WHERE r.kind = 'nm'
);

DELETE FROM nm_connects
WHERE ref_id IN (SELECT ref_id FROM refs WHERE kind = 'nm');

DELETE FROM nm_topology
WHERE ref_id IN (SELECT ref_id FROM refs WHERE kind = 'nm');

DELETE FROM nm_blocks
WHERE ref_id IN (SELECT ref_id FROM refs WHERE kind = 'nm');

DELETE FROM chunks
WHERE chunk_kind = 'card_combined'
  AND ref_id IN (SELECT ref_id FROM refs WHERE kind = 'nm');

UPDATE refs
SET retired_at = now()
WHERE kind = 'nm' AND retired_at IS NULL;

COMMIT;

-- End of 0007_units_nm_wipe.sql
