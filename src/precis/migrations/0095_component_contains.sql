-- 0095_component_contains.sql
--
-- The component assembly tree (docs/proposals/component-assembly-tree.md) —
-- register the `contains` / `part-of` relation pair that binds a component
-- to the components it structurally contains (a BOM edge, orthogonal to
-- `made-of`'s substance-composition edge from migration 0093):
--   * `contains`  (parent → child)  ↔ `part-of`
--
-- No new tables — edges live in `links` (quantity + optional reference
-- designator ride in `links.meta` as `{qty, ref}`, via
-- `add_link(merge_meta=True)`). Asymmetric, each with an inverse so both
-- directions auto-mirror at read time. The `Relation` Literal +
-- `_INVERSE_RELATIONS` map in store/types.py are kept in sync with this
-- seed (type-checkers catch a typo'd `relation=` ahead of the FK).
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('contains', FALSE, 'part-of',
     'Source component structurally contains target component (BOM edge).'),
    ('part-of',  FALSE, 'contains',
     'Source component is structurally part of target component.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0095_component_contains.sql
