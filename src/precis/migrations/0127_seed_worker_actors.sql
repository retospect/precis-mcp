-- 0127_seed_worker_actors.sql
--
-- Seed three worker actors that already write ``set_by`` values without a
-- backing ``actors`` row — they've only worked because those code paths
-- never hit the FK-enforcing columns (``links.set_by`` etc.). Making them
-- first-class lets ``ActorSlug`` (store/types.py) widen to cover them.
--
--   * `dream`  — the dreaming worker that mints speculative acquisitions
--     (src/precis/handlers/finding.py).
--   * `weave`  — the quest weave pass (src/precis/quest/weave.py).
--   * `orcid`  — the ORCID author-discovery stub minter
--     (src/precis/handlers/orcid.py, src/precis/handlers/paper.py).
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT (slug) DO NOTHING`),
-- matching the `actors_pkey` PRIMARY KEY (slug).

BEGIN;

INSERT INTO actors (slug, description) VALUES
    ('dream',
     'Dreaming worker — mints speculative acquisitions from existing '
     'findings/claims for later review.'),
    ('weave',
     'Quest weave pass — automated quest-graph maintenance and stitching.'),
    ('orcid',
     'ORCID author-discovery stub minter — creates stub author records '
     'from ORCID lookups.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0127_seed_worker_actors.sql
