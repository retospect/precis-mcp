-- 0040_cfp_requirement_relation.sql
--
-- Proposal writing (ADR: proposal-writing) — register the link relation
-- that binds a proposal-project todo to its call-for-proposal document:
--   * `has-requirement` (project todo → cfp ref) ↔ `requirement-of`
--
-- The planner follows this edge from a writing tick's project ancestry
-- to inject the call-for-proposal's required sections + word limits into
-- the prompt (workers/planner_prompt._m_requirements). Asymmetric, each
-- with an inverse so "what requires this cfp?" / "what cfp does this
-- project answer?" both auto-mirror.
--
-- Links FK into `relations`, and the `Relation` Literal + the
-- `_INVERSE_RELATIONS` map in store/types.py are kept in sync with this
-- seed (type-checkers catch a typo'd `rel=` ahead of the FK).
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('has-requirement', FALSE, 'requirement-of',
     'Source project (todo) must satisfy target call-for-proposal (cfp).'),
    ('requirement-of',  FALSE, 'has-requirement',
     'Source call-for-proposal (cfp) is a requirement of target project.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0040_cfp_requirement_relation.sql
