-- FK-covering indexes for the nm tables.
--
-- 0001/0002 indexed the hot reads with partial indexes
-- (``WHERE retired_at IS NULL``), but a partial index on that predicate
-- doesn't serve the FK-cascade lookup — a parent DELETE/UPDATE probes
-- the child by FK column over ALL rows, retired included, so every
-- refs/nm_blocks delete seq-scanned the children
-- (test_schema_design.py::test_fk_columns_have_covering_index). Plain
-- single-column indexes cover the cascades; the partials stay for the
-- live-row reads.

BEGIN;

CREATE INDEX IF NOT EXISTS nm_blocks_ref_fk_idx
    ON nm_blocks (ref_id);
CREATE INDEX IF NOT EXISTS nm_blocks_parent_fk_idx
    ON nm_blocks (parent_block_id);
CREATE INDEX IF NOT EXISTS nm_blocks_template_fk_idx
    ON nm_blocks (template_block_id);
CREATE INDEX IF NOT EXISTS nm_ports_block_fk_idx
    ON nm_ports (block_id);
CREATE INDEX IF NOT EXISTS nm_connects_ref_fk_idx
    ON nm_connects (ref_id);
CREATE INDEX IF NOT EXISTS nm_topology_ref_fk_idx
    ON nm_topology (ref_id);
CREATE INDEX IF NOT EXISTS nm_topology_subject_fk_idx
    ON nm_topology (subject_block);
CREATE INDEX IF NOT EXISTS nm_topology_object_fk_idx
    ON nm_topology (object_block);

COMMIT;
