-- 0022_se_pocket_class_hash.sql
--
-- Pocket-from-molecule slice 2b (docs/backlog/pocket-from-molecule.md):
-- se_pockets gains ``class_hash``, the pocket spec-class fingerprint —
-- sha256 over the canonical region list (sorted canonical selector
-- texts, each with its sorted (measurand, target, band) measure
-- tuples). Derived, never authored: recomputed by
-- precis_se.pockets.stamp_class_hashes on every apply_ops and persisted
-- here so a pocket's class can be queried without replaying the spec.
-- NULL for pockets written before the column existed.
--
-- Forward-only (ADR 0005). Squawk: additive, nullable — no backfill.

BEGIN;

ALTER TABLE se_pockets
    ADD COLUMN IF NOT EXISTS class_hash text;

COMMENT ON COLUMN se_pockets.class_hash IS
    'sha256 of the canonical region list (pocket-from-molecule 2b); derived, recomputed per apply_ops';

COMMIT;

-- End of 0022_se_pocket_class_hash.sql
