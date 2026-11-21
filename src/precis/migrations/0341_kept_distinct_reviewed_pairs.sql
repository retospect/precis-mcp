-- 0341_kept_distinct_reviewed_pairs.sql
-- Generalise the existing symmetric relation beyond BOM parts (gr676754).
-- Keep in sync with store/types.py::Relation.
BEGIN;

UPDATE relations
SET description = 'Two refs reviewed as possible duplicates but deliberately '
    'kept separate; links.meta.reason carries the reviewer''s why. '
    'Applies to any ref pair, including findings and BOM parts; suppresses '
    'the BOM consolidation nudge for the pair.'
WHERE slug = 'kept-distinct';

COMMIT;
