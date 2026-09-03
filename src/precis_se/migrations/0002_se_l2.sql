-- precis_se/0002_se_l2.sql
--
-- se slice 3 — the L2 invariant tier (docs/backlog/se-kind.md "L2 —
-- declared invariants"): named measures with tolerance relations, and a
-- loads slot on blocks. Joints need no DDL — `se_connects.joint` (0001)
-- was created for exactly this slice; its kinematic-class × mechanism
-- schema is enforced in code from this migration's ship onward.
--
-- Forward-only (ADR 0005): 0001 is sealed, so the blocks' loads column
-- arrives as an ALTER here. Idempotent. Plugin migration (namespace
-- `precis_se`), applied after core.

BEGIN;

-- 1. loads on blocks ----------------------------------------------------
-- Objective vectors (force/torque in real units, duty, cycles) — the
-- kind-neutral loads vocabulary (se-kind.md "Relation to nm": loads are
-- shared-core vocabulary; atomic systems bear the same loads at pN
-- scale). Connects already carry `objectives` since 0001.
ALTER TABLE se_blocks ADD COLUMN IF NOT EXISTS objectives jsonb;

-- 2. measures + tolerance relations -------------------------------------
-- One row per named measure on a block ("wheel.bore_d"). A tolerance is a
-- RELATION between measures, never an absolute number on one block
-- (se-kind.md L2): the relation lives ON its target measure —
-- `relation = {"source": "hub.od_d", "offset": 2e-4, "tol": 5e-5}` reads
-- "this = hub.od_d + 0.2 mm ± 0.05 mm". `value` is the independently
-- declared number (metres), nullable — a measure may exist as a named
-- handle before anyone commits a figure (suggestive by contract), and a
-- relation-only measure derives its value through stack-up. Strength is
-- the pcb_measures hard/soft/gauge triad: hard gates realization, soft is
-- an objective, gauge just reports.
--
-- Name-keyed like everything else (persist is retire-all/reinsert-all;
-- `block` and `relation.source` are text names, never block-row FKs). A
-- relation whose source doesn't (yet) exist is legal at write time —
-- forward references within one ops batch are normal workflow — and an
-- *unresolvable relation* is graph-DRC's finding at read time.
CREATE TABLE IF NOT EXISTS se_measures (
    id          bigserial PRIMARY KEY,
    ref_id      bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    block       text NOT NULL,
    name        text NOT NULL,
    -- declared value, metres (float64 everywhere — se-kind.md "Units").
    value       double precision,
    -- {"source": "block.measure", "offset": <m>, "tol": <m ≥ 0>} or NULL.
    relation    jsonb,
    strength    text NOT NULL DEFAULT 'gauge'
                CHECK (strength IN ('hard', 'soft', 'gauge')),
    -- the intent — "press-fit interference for the bearing seat".
    reason      text,
    retired_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE se_measures IS
    'se named measures + tolerance relations (docs/backlog/se-kind.md L2, '
    'the pcb_measures hard/soft/gauge posture): a relation ties its target '
    'measure to a source measure by offset ± tol; stack-up evaluation '
    'follows relation chains (precis_se.measures).';

-- a measure name is unique on its (live) block within the design.
CREATE UNIQUE INDEX IF NOT EXISTS se_measures_ref_block_name_key
    ON se_measures (ref_id, block, name) WHERE retired_at IS NULL;
-- plain (non-partial) so it also covers FK-cascade scans — the 0001 rule.
CREATE INDEX IF NOT EXISTS se_measures_ref_idx
    ON se_measures (ref_id);

COMMIT;

-- End of 0002_se_l2.sql
