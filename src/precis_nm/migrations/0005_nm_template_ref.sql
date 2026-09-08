-- precis_nm/0005_nm_template_ref.sql
--
-- Cross-design instancing (docs/backlog/blocktree-library-build-plan.md
-- slice 1): an ``instance_block``/``add_block`` ``template`` becomes
-- NAME-KEYED TEXT — a bare local block name (unchanged behaviour), or
-- ``<design-slug>#<block-name>`` naming a block in ANOTHER live ``nm``
-- design (a library part, placed by reference, never copied) — via a new
-- ``template_ref`` column, used going forward instead of the row-id FK
-- ``template_block_id`` (0001).
--
-- The FK never could have supported a cross-design reference:
-- ``nm_blocks.id`` is rebuilt on EVERY save (``persist.save_tree`` retires
-- the whole live tree for a ref and reinserts it fresh with new ids), and
-- even the LOCAL case only survived that because parent/template/ports are
-- all rewritten together, in the SAME transaction, from the SAME
-- ``name_to_id`` map built during that one save (persist.py's own
-- "Round-2 landmine" note). A cross-design reference has no such lockstep
-- to lean on — the two designs save independently, at different times, in
-- different transactions — so an id-keyed pointer into another design
-- would go stale the moment THAT design was next saved. ``nm_connects``
-- (migration 0002) already made exactly this call for its own endpoints,
-- for the same reason; this migration brings the block tree's own
-- reuse-by-reference edge in line with it.
--
-- ``template_block_id`` is left in place, forever NULL from this migration
-- forward, rather than dropped: 0004 (``nm_fk_indexes.sql``, sealed,
-- forward-only — never edited) unconditionally re-creates an index on it,
-- and this project's dark-kind plugin tests re-run EVERY migration file
-- from scratch on each test against a schema that already carries later
-- migrations' changes (there is no per-plugin ledger the way core
-- migrations get one) — dropping the column would make 0004's own index
-- creation fail the next time it replays. Leaving an unused nullable
-- column costs nothing at zero rows; deleting the column is not worth
-- fighting that replay order for.
--
-- Zero rows in ``nm_blocks`` in prod as of this migration (measured
-- 2026-09-07, same measurement the build plan cites).
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration
-- (namespace ``precis_nm``), applied after 0001-0004.

BEGIN;

ALTER TABLE nm_blocks ADD COLUMN IF NOT EXISTS template_ref text;

COMMENT ON COLUMN nm_blocks.template_ref IS
    'Instance template reference: a bare local block name, or '
    'design-slug#block-name naming a block in another live nm design '
    '(docs/backlog/blocktree-library-build-plan.md slice 1). Name-keyed '
    'text, resolved at read time — never a row-id FK (see this '
    'migration''s header for why). NULL = an ordinary, non-instance block. '
    'Supersedes template_block_id (0001), left in place unused (see this '
    'migration''s header).';

-- the hot read: does any live block anywhere reference THIS name as its
-- template (op_remove_block's "used as a template" guard, and the
-- cross-design resolver's own block-by-name lookup) — a plain (not
-- retired_at-partial) index, since a retired row's template_ref is
-- otherwise unindexed and this column is queried by value, not scanned
-- per-ref the way nm_blocks_ref_idx is.
CREATE INDEX IF NOT EXISTS nm_blocks_template_ref_idx
    ON nm_blocks (template_ref) WHERE template_ref IS NOT NULL;

COMMIT;

-- End of 0005_nm_template_ref.sql
