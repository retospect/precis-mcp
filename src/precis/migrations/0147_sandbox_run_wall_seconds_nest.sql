-- 0147_sandbox_run_wall_seconds_nest.sql
--
-- Vocabulary-compaction Stage C (docs/backlog/vocab-compaction-stages.md):
-- close out the Stage A "wall_seconds nested unification" read-both shim
-- (`workers/job_types/sandbox_run.py::resolve_wall_seconds`). Writers have
-- nested the sandbox_run job budget under `params.resources.wall_seconds`
-- since that migration; this backfills any surviving job row still carrying
-- only the legacy flat `params.wall_seconds` into the nested shape, so the
-- shim's read-only fallback can be deleted from code in the same commit.
--
-- Two-step `jsonb_set` because the target path's middle element
-- (`resources`) may not exist yet: the inner call ensures `params.resources`
-- is an object (creating it empty when absent, preserving it otherwise)
-- before the outer call sets `wall_seconds` under it — `jsonb_set` can only
-- create the FINAL path element, not intermediate ones. `#-` drops the
-- retired flat key in the same expression.
--
-- Only touches `kind='job'` rows that still have the flat key and don't
-- already have the nested one (idempotent — an already-migrated or fresh DB
-- matches nothing).
--
-- Forward-only (ADR 0005).

BEGIN;

-- The CASE (not COALESCE) guard on `resources` matters twice over: COALESCE
-- only substitutes on SQL NULL, so an explicit jsonb `null` value (a real,
-- distinct jsonb datum) would flow through and make the outer jsonb_set
-- fail on a non-object path. jsonb_typeof handles both absent (SQL NULL in)
-- and jsonb-null/scalar values.
UPDATE refs
   SET meta = jsonb_set(
                 jsonb_set(
                   meta #- '{params,wall_seconds}',
                   '{params,resources}',
                   CASE WHEN jsonb_typeof(meta->'params'->'resources') = 'object'
                        THEN meta->'params'->'resources'
                        ELSE '{}'::jsonb END
                 ),
                 '{params,resources,wall_seconds}',
                 meta->'params'->'wall_seconds'
               )
 WHERE kind = 'job'
   AND meta->'params' ? 'wall_seconds'
   AND NOT (COALESCE(meta->'params'->'resources', '{}'::jsonb) ? 'wall_seconds');

-- A row carrying BOTH keys (nested already authoritative) is skipped above;
-- drop its stale flat key too so no legacy key survives the stage.
UPDATE refs
   SET meta = meta #- '{params,wall_seconds}'
 WHERE kind = 'job'
   AND meta->'params' ? 'wall_seconds'
   AND COALESCE(meta->'params'->'resources', '{}'::jsonb) ? 'wall_seconds';

COMMIT;

-- End of 0147_sandbox_run_wall_seconds_nest.sql
