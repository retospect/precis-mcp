-- 0054_datasheet_of_relation.sql
--
-- Datasheet ↔ part linkage (ADR 0042 §7) — register the link relation that
-- binds a component datasheet to the part it documents:
--   * `datasheet-of`  (datasheet → part)  ↔ `has-datasheet`
--
-- A `datasheet` is an evidence-role ingested PDF (same Marker → chunks
-- pipeline as a paper). The part it documents is a catalog row; the edge
-- lets a part surface its datasheet ("what documents this part?") and a
-- datasheet name its subject ("what part is this for?"). The
-- `DatasheetHandler` docstring already advertises `link(rel='datasheet-of')`
-- — this seed is what makes that call resolve instead of hitting the FK.
--
-- Asymmetric, each with an inverse so both directions auto-mirror at read
-- time. Links FK into `relations`, and the `Relation` Literal +
-- `_INVERSE_RELATIONS` map in store/types.py are kept in sync with this seed
-- (type-checkers catch a typo'd `rel=` ahead of the FK).
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('datasheet-of',  FALSE, 'has-datasheet',
     'Source datasheet documents target part (evidence for its specs).'),
    ('has-datasheet', FALSE, 'datasheet-of',
     'Source part is documented by target datasheet.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0054_datasheet_of_relation.sql
