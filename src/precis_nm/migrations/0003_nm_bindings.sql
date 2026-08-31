-- precis_nm/0003_nm_bindings.sql
--
-- Slice 3 round 3 (docs/backlog/nm-kind.md "Slice 3 design"): the L5
-- binding (block -> structure design) plus the L2 threading columns
-- ``nm_topology`` actually needs to survive a save.
--
-- 1. block-level binding ------------------------------------------------
-- ``nm_blocks.bound_design`` is the block-level half of ``bind_structure``:
-- ONE fact ("this block is realised by structure design <slug>"), not a
-- link row — the per-port atom map already lives on ``nm_ports.bound_design``/
-- ``bound_atom`` (0001, unused until this round). A block column (not a
-- join table) mirrors how ``envelope``/``dof`` already live directly on the
-- block row: this is a property of the block, one value, NULL = unfilled.
--
-- 2. nm_topology name columns -------------------------------------------
-- 0001 gave ``nm_topology`` bigint FK columns (``subject_block`` NOT NULL,
-- ``object_block`` nullable) pointing at ``nm_blocks.id``. But
-- ``precis_nm.persist.save_tree`` is retire-all/reinsert-all: every save
-- soft-retires every live ``nm_blocks`` row for the ref and reinserts the
-- whole tree with brand-new ids (0001's own header; persist.py's "Round-2
-- landmine" note, which ``nm_ports`` solved by re-inserting in lockstep
-- with the fresh ids on every save). A ``nm_topology`` row id-FK'd the same
-- way would silently strand on the very next save — exactly the landmine
-- ``nm_connects`` (0002) sidestepped by going name-keyed instead of
-- id-keyed. Threading takes the same fix here: add ``subject_name``/
-- ``object_name`` text columns (block *names* — the tree's stable
-- identity, ops.py's module docstring), NOT NULL like 0002's
-- ``nm_connects`` name columns (both endpoints are always required for a
-- threading row, the same as a connect's two endpoints) — safe as a plain
-- ``ADD COLUMN ... NOT NULL`` because ``nm_topology`` is GUARANTEED EMPTY
-- at migration time: this table has existed since 0001 but no writer for
-- it has ever shipped (the `nm` kind is a dark plugin, and this is the
-- first round that touches ``nm_topology`` at all — see 0001's own header,
-- "created now (forward-only discipline: no later ALTER) but unused until
-- the handler wires them up"). ``subject_block``'s NOT NULL is also
-- relaxed, since the application uses ONLY the name columns from this
-- round on; ``subject_block``/``object_block`` are left in place,
-- unpopulated, rather than dropped — forward-only discipline (ADR 0005): a
-- sealed migration is never edited, and a column is dropped only in a
-- later migration if it's ever actually repurposed or removed.
--
-- Forward-only (ADR 0005). Idempotent. Plugin migration (namespace
-- `precis_nm`), applied after 0001/0002 (Migrator.discover_sources orders
-- by filename within a source). 0001/0002 are sealed; never edit them,
-- ship forward instead.

BEGIN;

ALTER TABLE nm_blocks ADD COLUMN IF NOT EXISTS bound_design text;

COMMENT ON COLUMN nm_blocks.bound_design IS
    'The L5 binding (docs/backlog/nm-kind.md): the structure design slug '
    'this block is realised by, via bind_structure. NULL = unfilled. One '
    'fact, not a link row -- the per-port atom map lives on nm_ports.';

ALTER TABLE nm_topology ADD COLUMN IF NOT EXISTS subject_name text NOT NULL;
ALTER TABLE nm_topology ADD COLUMN IF NOT EXISTS object_name text NOT NULL;
ALTER TABLE nm_topology ALTER COLUMN subject_block DROP NOT NULL;

COMMENT ON COLUMN nm_topology.subject_name IS
    'Name-keyed subject block (see this file''s header for why: the '
    'id-keyed subject_block/object_block columns break under '
    'save_tree''s id-rebuild). The application reads/writes ONLY this '
    'column and object_name from round 3 on.';
COMMENT ON COLUMN nm_topology.object_name IS
    'Name-keyed object block -- see subject_name.';

-- One live threading row per exact (subject_name, object_name) pair per
-- design -- directional ("a threaded through b" != "b threaded through
-- a"), so this is an ORDERED-pair index, unlike nm_connects' canonicalized
-- unordered one (0002's index comment).
CREATE UNIQUE INDEX IF NOT EXISTS nm_topology_threading_pair_key
    ON nm_topology (ref_id, subject_name, object_name)
    WHERE retired_at IS NULL AND kind = 'threading';

COMMIT;

-- End of 0003_nm_bindings.sql
