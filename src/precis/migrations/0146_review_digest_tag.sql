-- 0146_review_digest_tag.sql
--
-- Vocabulary-compaction Stage C (docs/backlog/vocab-compaction-stages.md):
-- the review-driver's open-tag literals `tier:structural` / `tier:deep`
-- (marking a `kind='memory'` digest) collapse `tier` -> `digest`, matching
-- the code field rename `Reviewer.tier_tag` -> `Reviewer.digest_tag`
-- (`workers/review.py`). These are digest CADENCE markers (which reviewer
-- wrote it), not a capability/fidelity tier — the old name was borrowed,
-- not owned.
--
-- Merge-then-delete pattern (0102's `level:proposed-tactical` precedent):
-- `tags` has a UNIQUE(namespace, value), so a raw value UPDATE risks
-- colliding with a `digest:*` row minted between migration-write time and
-- deploy (unlikely for an internal marker, but the pattern costs nothing
-- extra and is already the house idiom). `ON DELETE CASCADE` on
-- `ref_tags`/`chunk_tags` -> `tags` sweeps up the retired rows.
--
-- Idempotent: an already-migrated DB (or a fresh one) has no `tier:*` tag
-- rows, so every statement below is a no-op.
--
-- Forward-only (ADR 0005).

BEGIN;

INSERT INTO tags (namespace, value)
SELECT 'OPEN', 'digest:structural'
WHERE EXISTS (SELECT 1 FROM tags WHERE namespace = 'OPEN' AND value = 'tier:structural')
ON CONFLICT (namespace, value) DO NOTHING;

INSERT INTO tags (namespace, value)
SELECT 'OPEN', 'digest:deep'
WHERE EXISTS (SELECT 1 FROM tags WHERE namespace = 'OPEN' AND value = 'tier:deep')
ON CONFLICT (namespace, value) DO NOTHING;

UPDATE ref_tags rt
   SET tag_id = (SELECT tag_id FROM tags WHERE namespace = 'OPEN' AND value = 'digest:structural')
 WHERE rt.tag_id = (SELECT tag_id FROM tags WHERE namespace = 'OPEN' AND value = 'tier:structural')
   AND NOT EXISTS (
       SELECT 1 FROM ref_tags x
        WHERE x.ref_id = rt.ref_id
          AND x.tag_id = (SELECT tag_id FROM tags WHERE namespace = 'OPEN' AND value = 'digest:structural')
   );

UPDATE ref_tags rt
   SET tag_id = (SELECT tag_id FROM tags WHERE namespace = 'OPEN' AND value = 'digest:deep')
 WHERE rt.tag_id = (SELECT tag_id FROM tags WHERE namespace = 'OPEN' AND value = 'tier:deep')
   AND NOT EXISTS (
       SELECT 1 FROM ref_tags x
        WHERE x.ref_id = rt.ref_id
          AND x.tag_id = (SELECT tag_id FROM tags WHERE namespace = 'OPEN' AND value = 'digest:deep')
   );

DELETE FROM tags
 WHERE namespace = 'OPEN' AND value IN ('tier:structural', 'tier:deep');

COMMIT;

-- End of 0146_review_digest_tag.sql
