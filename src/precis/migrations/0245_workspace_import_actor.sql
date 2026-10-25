-- 0245_workspace_import_actor.sql
--
-- Seed the ``workspace_import`` actor the se workspace importer already
-- stamps as ``set_by`` (``precis_se.workspace_import``: the fork's
-- ``derived-from`` add_link, the folder's ``part-of`` link written by
-- the ``import_workspace`` se op). ``links.set_by`` is FK-enforced to
-- ``actors``, so the fork path's edge insert could never have committed
-- — the op door is the first caller to reach it.
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT (slug) DO NOTHING`),
-- matching the `actors_pkey` PRIMARY KEY (slug).

BEGIN;

INSERT INTO actors (slug, description) VALUES
    ('workspace_import',
     'Structure-workspace importer — imports a workspace run''s out/ '
     'tree into an se design (precis_se.workspace_import).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0245_workspace_import_actor.sql
