-- precis_se/0003_se_bom.sql
--
-- se off-the-shelf rung 1 (docs/backlog/se-off-the-shelf-fabrication.md
-- "Ship order" 1): **things you buy instead of making**. Two halves, both
-- pure L0/L3 bookkeeping — no geometry, which is exactly why this rung
-- doesn't wait on the profile tier:
--
--   * `se_bom` — a bought item hung off a block or a connect ("ball
--     bearing ×2 between hub and wheel"). se-kind.md's Decisions are
--     explicit that a thing *bought* is never a block: it is a
--     `component`/`part` link with a quantity. The rollup multiplies
--     quantities through the block tree's array multiplicities
--     (`precis_se.bom`) and reaches `component`'s existing cost/mass
--     values for the totals — nothing of that rollup is reimplemented.
--   * `se_blocks.bound_kind` grows `'component'`/`'part'` — the L3
--     realization "a bought part with a datasheet envelope". 0001 is
--     sealed (ADR 0005 forward-only), so the CHECK is dropped and re-added
--     here rather than edited there.
--
-- Name-keyed throughout: `save_tree` is retire-all/reinsert-all, so a BOM
-- line addresses its target by block/connect *name*, never by a block row
-- id (0001's "Round-2 landmine" rule).
--
-- Forward-only (ADR 0005). Idempotent. Plugin migration (namespace
-- `precis_se`), applied after core.

BEGIN;

-- 1. bought items on the tree --------------------------------------------
CREATE TABLE IF NOT EXISTS se_bom (
    id          bigserial PRIMARY KEY,
    ref_id      bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    -- the target, exactly one form (CHECK below): a block by name, or a
    -- connect by its four name-keyed endpoints (stored canonically —
    -- sorted endpoint pairs, matching se_connects' save-time ordering, so
    -- the same connect is always the same tuple).
    block       text,
    a_block     text,
    a_port      text,
    b_block     text,
    b_port      text,
    -- what is bought: a `component` slug (the engineering store) or a
    -- `part` C-number (the LCSC/JLCPCB catalog). Text, resolved at read
    -- time — a BOM line naming a component that doesn't exist yet is a
    -- legal, honest state (suggestive by contract) and a DRC finding, not
    -- a write-time rejection.
    item_kind   text NOT NULL CHECK (item_kind IN ('component', 'part')),
    item        text NOT NULL,
    -- quantity per *one* occurrence of the target. float64 because a
    -- bought item may be continuous (2.4 m of pipe, 30 mL of adhesive) —
    -- the item's own uom says which; `uom` here overrides it for this
    -- line only.
    qty         double precision NOT NULL DEFAULT 1 CHECK (qty > 0),
    uom         text,
    reason      text,
    retired_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT se_bom_one_target CHECK (
        (block IS NOT NULL
         AND a_block IS NULL AND a_port IS NULL
         AND b_block IS NULL AND b_port IS NULL)
        OR
        (block IS NULL
         AND a_block IS NOT NULL AND a_port IS NOT NULL
         AND b_block IS NOT NULL AND b_port IS NOT NULL)
    )
);

COMMENT ON TABLE se_bom IS
    'se bill of materials (docs/backlog/se-off-the-shelf-fabrication.md): '
    'a bought component/part hung off a block or a connect, name-keyed, '
    'with a per-occurrence quantity. The design''s array multiplicities '
    'turn per-occurrence into total at read time (precis_se.bom); cost/'
    'mass totals come from the component kind''s own spec values.';

-- one live line per (target, item) — the ops layer merges a repeat add
-- into the existing line, this is the backstop.
CREATE UNIQUE INDEX IF NOT EXISTS se_bom_target_item_key
    ON se_bom (
        ref_id,
        COALESCE(block, ''),
        COALESCE(a_block, ''), COALESCE(a_port, ''),
        COALESCE(b_block, ''), COALESCE(b_port, ''),
        item_kind, item
    ) WHERE retired_at IS NULL;
-- plain (non-partial) so it also covers FK-cascade scans — the 0001 rule.
CREATE INDEX IF NOT EXISTS se_bom_ref_idx
    ON se_bom (ref_id);

-- 2. L3 binding to a bought part -----------------------------------------
-- 0001 declared `bound_kind IN ('cad','nm')` — the two *designed*
-- realizations. A bought part is the third: the block's solid comes from a
-- catalog row, not from anything anyone authored here.
-- `NOT VALID` with no follow-up VALIDATE, deliberately: the new CHECK is
-- strictly *looser* than the one it replaces ('cad','nm' ⊂ the new set),
-- so every existing row provably satisfies it already and the back-scan
-- would only be ceremony. NOT VALID skips exactly that scan — enforcement
-- on every INSERT/UPDATE from here on is unaffected.
ALTER TABLE se_blocks DROP CONSTRAINT IF EXISTS se_blocks_bound_kind_check;
ALTER TABLE se_blocks ADD CONSTRAINT se_blocks_bound_kind_check
    CHECK (bound_kind IN ('cad', 'nm', 'component', 'part')) NOT VALID;

COMMIT;

-- End of 0003_se_bom.sql
