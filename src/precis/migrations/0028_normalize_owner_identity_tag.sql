-- 0028: normalize the legacy bare `OPEN/reto` identity tag onto the
-- canonical `OPEN/user:elmsfeuer`.
--
-- Part of the user-identity generalization (de-hardcode "reto" — the
-- same human was `reto` in code, `user:elmsfeuer` in the tag data, and
-- `owner` in the web source convention). The canonical handle is
-- `elmsfeuer` (it already has 44 live rows; zero rename needed there).
-- This migration only touches the one stray bare `reto` open tag.
-- See docs/design/user-identity-and-ask-routing.md.
--
-- A plain UPDATE of the value can't work: `OPEN/user:elmsfeuer` may
-- already exist, and `tags_namespace_value_key UNIQUE (namespace,
-- value)` would reject the collision. So we *merge* — repoint the
-- ref_tags rows, then drop the legacy tag (the ON DELETE CASCADE FK
-- sweeps up any leftover ref_tags / chunk_tags rows).
--
-- Idempotent + a full no-op on DBs without the `reto` tag (fresh
-- installs, the test DB): the guarded INSERT, the repoint UPDATE, and
-- the DELETE all match zero rows.

BEGIN;

-- Ensure the canonical tag exists — but only when there is actually a
-- `reto` tag to migrate, so a fresh DB never gets a spurious row.
INSERT INTO tags (namespace, value)
SELECT 'OPEN', 'user:elmsfeuer'
WHERE EXISTS (
    SELECT 1 FROM tags WHERE namespace = 'OPEN' AND value = 'reto'
)
ON CONFLICT (namespace, value) DO NOTHING;

-- Repoint every ref carrying the legacy `reto` tag onto
-- `user:elmsfeuer`, skipping any ref that already carries it (the
-- UNIQUE (ref_id, tag_id) shape of ref_tags forbids a duplicate).
UPDATE ref_tags rt
   SET tag_id = (
       SELECT tag_id FROM tags WHERE namespace = 'OPEN' AND value = 'user:elmsfeuer'
   )
 WHERE rt.tag_id = (
       SELECT tag_id FROM tags WHERE namespace = 'OPEN' AND value = 'reto'
   )
   AND NOT EXISTS (
       SELECT 1 FROM ref_tags x
        WHERE x.ref_id = rt.ref_id
          AND x.tag_id = (
              SELECT tag_id FROM tags WHERE namespace = 'OPEN' AND value = 'user:elmsfeuer'
          )
   );

-- Drop the legacy tag. ON DELETE CASCADE (ref_tags / chunk_tags →
-- tags) removes any rows that still pointed at it (refs that already
-- carried user:elmsfeuer and so were skipped above).
DELETE FROM tags WHERE namespace = 'OPEN' AND value = 'reto';

COMMIT;
