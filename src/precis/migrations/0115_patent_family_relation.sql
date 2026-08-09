-- 0115_patent_family_relation.sql
--
-- Patent-family stubbing (docs/backlog/patent-evidence-parity.md Phase 2).
-- `same-family-as` binds a stub patent ref (biblio meta only, no
-- description/claim blocks) to the family's current representative — the
-- earliest-published already-ingested member (`_patent_family.py`). Written
-- by `_patent_ingest.py` when a fresh ingest lands in an already-ingested
-- DOCDB *simple* family (same `family_id` AND identical priority-claim set;
-- differing priority data always gets a full ingest, never a stub).
--
-- SYMMETRIC, no inverse (like `related-to` / `touched`) — family membership
-- has no direction, and the write side always fires from the newer stub so
-- one row per pair is enough; a reader can query either endpoint via
-- `links_for(direction='both')`. `links.relation` FKs to `relations(slug)`
-- (`links_relation_fkey`) — a closed, migration-seeded vocabulary, not free
-- text (same trap as `establishes`/`refines`/`awaits-evidence`, migrations
-- 0094/0100/0105). Keep in sync with the `Relation` Literal in
-- `store/types.py`.
--
-- Forward-only. Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('same-family-as', TRUE, NULL,
     'Both patent refs are members of the same EPO OPS DOCDB patent '
     'family; source is typically a stub ingest, target the family''s '
     'current publication-date representative.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0115_patent_family_relation.sql
