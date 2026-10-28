-- Part-selection decisions live in memory; architecture decisions use
-- derived-from / serves instead. No inverse: read incoming edges on the part.
-- Existing related-to interim links are rewritten only on Reto's go.
BEGIN;

INSERT INTO relations
    (slug, is_symmetric, inverse_slug, description, domain_kinds, range_kinds)
VALUES
    ('selects', FALSE, NULL,
     'Memory selection record chooses this part or component.',
     ARRAY['memory'], ARRAY['part', 'component']),
    ('rejects', FALSE, NULL,
     'Memory selection record rejects this part or component; links.meta.reason is required.',
     ARRAY['memory'], ARRAY['part', 'component'])
ON CONFLICT (slug) DO NOTHING;

COMMIT;
