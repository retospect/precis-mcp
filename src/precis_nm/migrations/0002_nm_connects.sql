-- precis_nm/0002_nm_connects.sql
--
-- Slice 3 round 2 (docs/backlog/nm-kind.md "Round-2 constraint"): port↔port
-- intent edges (bond / non-bonded interaction). Ports land this round too,
-- but `nm_ports` was already created (unused) by 0001 — this migration
-- only adds the table 0001 didn't anticipate: connects.
--
-- **Endpoints are NAME-keyed (a_block/a_port/b_block/b_port text), never a
-- foreign key to nm_blocks.id.** `precis_nm.persist.save_tree` is
-- retire-all/reinsert-all: every save soft-retires every live `nm_blocks`
-- row for the ref and reinserts the whole tree with brand-new ids
-- (0001_nm_kind.sql's own header; persist.py's "Round-2 landmine" note). A
-- connect keyed by `block_id` would silently strand on the very next save
-- of the same design — the FK would still resolve (it'd point at a
-- *retired* block row, not a missing one), so nothing would ever error;
-- the connect would just quietly stop meaning anything. Scoping by
-- `ref_id` + block/port *names* instead sidesteps the whole problem: names
-- survive a save (they're the tree's stable identity — see ops.py's module
-- docstring), so a connect never needs to be re-pointed at a new block row
-- at all. `precis_nm.persist.save_tree` still retires and reinserts every
-- live connect on each save (in lockstep with the blocks, same
-- transaction) purely to keep the table's `retired_at` bookkeeping
-- consistent with the rest of the design — not because the names would
-- otherwise go stale.
--
-- Forward-only (ADR 0005). Idempotent. Plugin migration (namespace
-- `precis_nm`), applied after 0001 (Migrator.discover_sources orders by
-- filename within a source) — so `nm_blocks`/`nm_ports`/`refs` already
-- exist. 0001 is sealed; never edit it, ship forward instead.

BEGIN;

CREATE TABLE IF NOT EXISTS nm_connects (
    id            bigserial PRIMARY KEY,
    ref_id        bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    -- NAME-keyed endpoints — see this file's header for why never block_id.
    a_block       text NOT NULL,
    a_port        text NOT NULL,
    b_block       text NOT NULL,
    b_port        text NOT NULL,
    kind          text NOT NULL DEFAULT 'bond' CHECK (kind IN ('bond', 'interaction')),
    -- the objective-vector slot (e.g. target bond length/angle), and the
    -- {'role': ...} capability-gate override ops.py's connect op reads —
    -- free passthrough at this round, no schema enforced yet.
    objectives    jsonb,
    meta          jsonb,
    retired_at    timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE nm_connects IS
    'nm port-to-port intent edges (docs/backlog/nm-kind.md slice 3 round '
    '2): a bond or non-bonded interaction between two block.port endpoints, '
    'NAME-keyed (never nm_blocks.id — see this file''s header) so a save_tree '
    'id-rebuild can never strand one.';

-- One live connect per unordered endpoint pair per design. ops.py's
-- connect/disconnect ops canonicalize (a, b) by sorting the two endpoints
-- before this row is written (precis_nm.persist), so this ordered-tuple
-- index is sufficient to enforce the *unordered* uniqueness the ops layer
-- promises — see ops.py's `_connects_endpoint_pair`.
CREATE UNIQUE INDEX IF NOT EXISTS nm_connects_live_pair_key
    ON nm_connects (ref_id, a_block, a_port, b_block, b_port)
    WHERE retired_at IS NULL;

CREATE INDEX IF NOT EXISTS nm_connects_ref_idx
    ON nm_connects (ref_id) WHERE retired_at IS NULL;

COMMIT;

-- End of 0002_nm_connects.sql
