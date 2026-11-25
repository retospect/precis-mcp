-- 0370_anchor_fk_indexes.sql
--
-- Covering indexes for four FKs added without one (td773489 chunk_corrections
-- in 0358, td772423 anchor side tables in 0360), flagged by
-- tests/test_schema_design.py::test_fk_columns_have_covering_index. Without
-- them a parent DELETE seq-scans the child to find rows to cascade or null:
--
--   * chunk_corrections.chunk_id -> chunks (ON DELETE SET NULL). Body chunks
--     are DELETE + INSERT on re-ingest, so every chunk delete probes this
--     column. Partial on ``chunk_id IS NOT NULL``: the pointer is optional
--     and the probe is an equality on a non-null value.
--   * measure_anchors.anchor_id, evidence_anchors.anchor_id and
--     code_anchor_aliases.anchor_id -> anchors (ON DELETE CASCADE). The
--     primary keys lead with measure_id / link_id / ref_id, so the anchor
--     side has no usable index.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS chunk_corrections_chunk_id_idx
    ON chunk_corrections (chunk_id) WHERE chunk_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS measure_anchors_anchor_id_idx
    ON measure_anchors (anchor_id);
CREATE INDEX IF NOT EXISTS evidence_anchors_anchor_id_idx
    ON evidence_anchors (anchor_id);
CREATE INDEX IF NOT EXISTS code_anchor_aliases_anchor_id_idx
    ON code_anchor_aliases (anchor_id);

COMMIT;

-- End of 0370_anchor_fk_indexes.sql
