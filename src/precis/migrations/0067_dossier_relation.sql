-- 0067_dossier_relation.sql
--
-- The `dossier-of` / `has-dossier` link relation (quest layer, slice 4a —
-- docs/proposals/quest-layer.md §Two memories). A quest keeps TWO records: the
-- append-only `quest_log` LOGBOOK (migration 0065) and a DOSSIER — a `draft` the
-- quest owns, the living synthesis rewritten every research cycle (current
-- understanding · best leads · what's ruled out · open questions). The dossier
-- doubles as the autonomous loop's ROLLING CONTEXT: each tick reads the compact
-- dossier instead of the whole logbook, so context stays bounded.
--
-- Modeled 1:1 on a project's `draft-of` (migration 0032): asymmetric,
-- auto-mirrored, one dossier per quest (the `create_draft` dup-guard enforces
-- the 1:1). Kept in sync with the `Relation` Literal + `_INVERSE_RELATIONS` in
-- store/types.py.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('dossier-of',  FALSE, 'has-dossier',
     'Source draft is the research dossier of the target quest — the living '
     'synthesis rewritten each cycle, and the loop''s rolling context.'),
    ('has-dossier', FALSE, 'dossier-of',
     'Source quest has the target draft as its research dossier.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0067_dossier_relation.sql
