-- 0173_taxon_kind.sql
--
-- The `taxon` kind — a node in the term taxonomy (docs/backlog/
-- term-taxonomy.md, stage A). A numeric-id ref on the `concept` pattern
-- (migration 0063): `title` = the print label, `meta` carries the fixed
-- node key set (definition, aliases, status, start, contract,
-- dimension_kind, si_vector, ...), and the only chunk is the reused
-- `card_combined` (ord=-1) so a node is a vector in the corpus manifold.
-- There is no ord 0 body chunk. No tables, indexes or views: hierarchy
-- reuses the existing `generalises` / `specialises` pair (seeded in 0001).
--
-- Registers one relation pair (kept in sync with the `Relation` Literal +
-- `_INVERSE_RELATIONS` in store/types.py):
--   instance-of <-> has-instance — a ref is a member of a taxon.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('taxon', TRUE, 'Taxon',
     'A node in the term taxonomy: a named term with an embeddable '
     'definition, an earned status (proposed / systematic), an optional '
     'dimension (dimension_kind + si_vector) and, on start nodes, a '
     'required-key contract. Body is ''<name> - <definition>''. See '
     'term-taxonomy.md.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('instance-of',  FALSE, 'has-instance',
     'Source ref is a member of the target taxon (e.g. a material, a measured value -> the term it instantiates).'),
    ('has-instance', FALSE, 'instance-of',
     'Source taxon has the target ref as a member.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0173_taxon_kind.sql
