-- 0088_draft_copy_of_relation.sql
--
-- Draft fork/deep-copy primitive: register the `copy-of` / `has-copy`
-- provenance relation a fork_draft() copy stamps on the new ref pointing
-- back at its source (put(kind='draft', copy_of='<slug>', project=…)).
--
-- Asymmetric, `has-copy` inverse so "what copies exist of this draft?"
-- auto-mirrors at read time (links_for), like `draft-of` / `has-draft`
-- (0032_draft_relations.sql).
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('copy-of',  FALSE, 'has-copy',
     'Source draft is a fork/deep-copy of target draft (chunks + links copied).'),
    ('has-copy', FALSE, 'copy-of',
     'Source draft has target draft as a fork/deep-copy of itself.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0088_draft_copy_of_relation.sql
