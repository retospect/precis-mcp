-- 0333_prior_art_relations.sql
-- Prior-art decisions record adoption or rejection, not evidential support.
-- The md workspace index has no corpus refs; its stored files are markdown.
BEGIN;

INSERT INTO relations
    (slug, is_symmetric, inverse_slug, description, domain_kinds, range_kinds)
VALUES
    ('inspired-by', FALSE, 'inspired',
     'Source idea or design was inspired by target prior art.',
     ARRAY['gripe', 'todo', 'memory', 'markdown'], ARRAY['paper', 'web', 'memory']),
    ('inspired', FALSE, 'inspired-by',
     'Source prior art inspired target idea or design.',
     ARRAY['paper', 'web', 'memory'], ARRAY['gripe', 'todo', 'memory', 'markdown']),
    ('rejected-after', FALSE, NULL,
     'Source records a decision against an approach after reading target prior art.',
     ARRAY['gripe', 'todo', 'memory', 'markdown'], ARRAY['paper', 'web', 'memory'])
ON CONFLICT (slug) DO NOTHING;

COMMIT;
