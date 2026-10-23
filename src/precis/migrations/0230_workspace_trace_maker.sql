-- 0230_workspace_trace_maker.sql
--
-- Structure workspaces slice 0 (docs/backlog/structure-workspaces.md
-- §Construction traces): every entity carries one small int, the run-local
-- id of the trace frame that created it — atoms and bonds get a `maker`
-- column so the provenance survives save/load and rides relax versions
-- (a relax write-back reinserts atom rows under the next version; the
-- column copies through with the rest of the row). NULL = no maker —
-- every pre-existing row and every non-workspace structure.
--
-- Also registers the two chunk kinds the workspace importer stores on the
-- run (or, for a containerless/manual import, on the se design ref):
-- `ws_trace` (the run's trace.json, one chunk) and `ws_manifest` (the
-- run's manifest.json — it carries the se block/port/connect frame ids).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE struct_atoms ADD COLUMN IF NOT EXISTS maker integer;
ALTER TABLE struct_bonds ADD COLUMN IF NOT EXISTS maker integer;

COMMENT ON COLUMN struct_atoms.maker IS
    'Run-local id (index into the run''s stored trace.json frames) of the '
    'construction-trace frame that created this atom — structure-workspaces '
    'provenance. NULL for hand-authored and generator-built atoms.';

COMMENT ON COLUMN struct_bonds.maker IS
    'Run-local trace frame id that created this bond — same contract as '
    'struct_atoms.maker.';

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('ws_trace', false,
     'A workspace run''s construction trace (trace.json): the frame tree '
     'entity `maker` columns index into. Stored once per run.'),
    ('ws_manifest', false,
     'A workspace run''s manifest.json: the imported block list plus the '
     'se block/port/connect frame ids.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0230_workspace_trace_maker.sql
