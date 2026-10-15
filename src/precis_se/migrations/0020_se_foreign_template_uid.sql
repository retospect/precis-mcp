-- precis_se/0020_se_foreign_template_uid.sql
--
-- A cross-design template reference ('design-slug#block-name') now stores
-- the foreign block's uid in se_blocks.template_uid too
-- (docs/backlog/design-state-core.md item 2, the cross-design half that
-- 0009 deferred). A uid is unique only within one design — branch copies
-- share them — so the join key is (design slug from template_ref, uid):
-- precis_se.persist writes it at save time and renders the reference back
-- from the foreign block's CURRENT label at load time, so an instance
-- follows its template across a relabel. The name text stays the fallback
-- for a dangling reference, exactly as for a local one.
--
-- Backfill: live cross-design rows whose target resolves today. Prod held
-- zero such rows when this shipped (2026-10-15); the statement is here for
-- every other database.

UPDATE se_blocks b
   SET template_uid = f.uid
  FROM (
        SELECT i.id_value AS slug, fb.name, fb.uid
          FROM se_blocks fb
          JOIN refs r ON r.ref_id = fb.ref_id AND r.kind = 'se'
                     AND r.retired_at IS NULL
          JOIN ref_identifiers i ON i.ref_id = r.ref_id
                                AND i.id_kind = 'cite_key'
         WHERE fb.retired_at IS NULL AND fb.uid IS NOT NULL
       ) f
 WHERE b.retired_at IS NULL
   AND b.template_uid IS NULL
   AND position('#' in b.template_ref) > 0
   AND f.slug = btrim(split_part(b.template_ref, '#', 1))
   AND f.name = btrim(substr(b.template_ref, position('#' in b.template_ref) + 1));

COMMENT ON COLUMN se_blocks.template_uid IS
    'The uid of the block template_ref names: a LOCAL reference''s block in '
    'this design, or — for a cross-design ''slug#block'' reference — that '
    'block in the design the slug names (uids are unique per design, so '
    'the slug in template_ref is part of the key). NULL when the reference '
    'did not resolve at save time; template_ref''s name text is then the '
    'fallback.';
