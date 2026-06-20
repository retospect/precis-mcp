-- 0032_draft_relations.sql
--
-- ADR 0032 — register the link relations the `draft` kind needs:
--   * `draft-of`    (draft ref → its project todo; 1:1) ↔ `has-draft`
--   * `snapshot-of` (a freeze → the draft it snapshots)  ↔ `has-snapshot`
--
-- Links FK into `relations`, and the `Relation` Literal in
-- store/types.py is kept in sync with this seed (type-checkers catch a
-- typo'd `rel=` ahead of the FK). Asymmetric, each with an inverse so
-- backlink queries ("what drafts this project?", "what snapshots this
-- draft?") auto-mirror.
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('draft-of',     FALSE, 'has-draft',
     'Source draft is the working document of target project (todo).'),
    ('has-draft',    FALSE, 'draft-of',
     'Source project (todo) has target draft as its working document.'),
    ('snapshot-of',  FALSE, 'has-snapshot',
     'Source frozen ref is a point-in-time snapshot of target draft.'),
    ('has-snapshot', FALSE, 'snapshot-of',
     'Source draft has target frozen ref as a snapshot.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0032_draft_relations.sql
