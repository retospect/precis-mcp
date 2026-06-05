-- 0003_drop_legacy_segments.sql
--
-- Drop the dead persistent-discovery-layer tables that survived the
-- F20 transition. As of v8.4.x no code path reads these:
--
-- * ``ref_segments`` — was populated by the retired ``segment_toc``
--   worker (deleted in v8.2.0); the new dynamic TOC computes clusters
--   from ``chunks.keywords`` at request time.
-- * ``ref_segment_sentences`` — was the source for search-hit excerpt
--   sub-lines under each result row; F20 replaced those with per-chunk
--   keyword displays.
--
-- The 0002_chunk_keywords migration deliberately left these in place
-- so older MCP server images (pre-v8.2.0) kept rendering through the
-- traffic cutover. After v8.4.3 is deployed everywhere they're pure
-- orphans — wasting disk + index pages, and making the schema diff
-- between fresh installs and migrated installs harder to reason
-- about.
--
-- Idempotent via ``IF EXISTS``: a fresh DB (which got the squashed
-- ``0001_initial.sql`` that already omits these tables) skips both
-- statements cleanly.
--
-- CASCADE drops dependent indexes + FKs in one step. There are no
-- other tables that reference these, so the CASCADE blast radius is
-- contained to the two tables themselves.

DROP TABLE IF EXISTS ref_segment_sentences CASCADE;
DROP TABLE IF EXISTS ref_segments CASCADE;

-- End of 0003_drop_legacy_segments.sql
