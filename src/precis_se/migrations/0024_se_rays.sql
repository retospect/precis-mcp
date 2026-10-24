-- precis_se/0024_se_rays.sql
--
-- Structure-workspaces slice 2r (docs/backlog/structure-workspaces.md
-- §Observation rays): a block carries named observation rays — the
-- multiscale §5.5 bookmark made concrete (precis_se.rays).
--
-- * `se_rays` — shaped exactly like `se_pockets`: a row per (block row,
--   name), written in lockstep with the freshly minted block id on every
--   retire-all/reinsert-all save, retired through the block.
--   `definition` is the ray as authored (origin, direction, length,
--   kind, fields, step, reach, frame, anchor, made_by); `digests` the
--   last five observation digests (event hash, structure versions,
--   pi_key, key numbers). The event list itself is never stored —
--   reads recompute it.
--
-- The migration bodies get `BEGIN;`/`COMMIT;` stripped and executed
-- inside the caller's transaction (precis_se/migrations/__init__.py), so
-- every statement below stays independently runnable.
--
-- Forward-only. Idempotent. Additive — no backfill. A PLUGIN migration
-- (namespace `precis_se`), applied after 0001-0023.

BEGIN;

CREATE TABLE IF NOT EXISTS se_rays (
    id          bigserial PRIMARY KEY,
    block_id    bigint NOT NULL REFERENCES se_blocks (id) ON DELETE CASCADE,
    name        text NOT NULL,
    definition  jsonb NOT NULL DEFAULT '{}'::jsonb,
    digests     jsonb NOT NULL DEFAULT '[]'::jsonb,
    retired_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE se_rays IS
    'se observation rays (precis_se.rays): a stored ray definition plus '
    'its last observation digests, one row per (block row, name), written '
    'in lockstep with se_blocks like se_pockets. Events are recomputed on '
    'read, never stored.';

CREATE UNIQUE INDEX IF NOT EXISTS se_rays_block_name_key
    ON se_rays (block_id, name) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS se_rays_block_idx
    ON se_rays (block_id);

COMMIT;

-- End of 0024_se_rays.sql
