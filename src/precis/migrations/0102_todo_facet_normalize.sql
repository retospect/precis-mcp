-- 0102_todo_facet_normalize.sql
--
-- §M facet normalization (docs/proposals/cluster-scheduling.md Pillar 1
-- §M) — the todo-collapse's narrow forward-only residue. Collapses the
-- `level:` 3-enum (strategic / tactical / subtask) into two explicit
-- boolean `meta` fields, drops the redundant `level:recurring` tag
-- (readers now key on `meta.schedule` presence), demotes the `LLM:*`
-- auto-run/model-tier tag to a single `meta.llm_tier` field, and folds
-- the never-gated `level:proposed-tactical` suggestion tag down to a
-- plain `proposed-tactical` open tag (prefix dropped, same semantics —
-- it was never part of the owner-only gradient).
--
-- Mapping (see handlers/_todo_guards.py's module docstring for the
-- full 2x2 table):
--   level:strategic  -> meta.rotation_root = true
--   level:tactical   -> meta.worker_mintable = false
--   level:subtask    -> no meta change (already the default: both facet
--                       keys absent, worker_mintable defaults true)
--   level:recurring  -> no meta change (redundant with meta.schedule,
--                       which every real recurring already carries —
--                       the Watches umbrella is the one exception, and
--                       it has no meta.schedule either, so dropping the
--                       tag leaves it correctly matching "not recurring,
--                       just a folder")
--   level:proposed-tactical -> merge onto the plain `proposed-tactical`
--                       open tag (0028's merge-then-delete pattern —
--                       ref_tags UNIQUE(ref_id, tag_id) forbids a raw
--                       repoint into a value that might already exist)
--   LLM:<value>      -> meta.llm_tier = <value>
--
-- All UPDATEs are idempotent (each only touches rows still carrying the
-- source tag); the closing DELETEs cascade to `ref_tags` / `chunk_tags`
-- via the FK, so nothing is orphaned. A fresh DB (no legacy rows) runs
-- every statement as a no-op.

BEGIN;

-- level:strategic -> meta.rotation_root = true
UPDATE refs r
   SET meta = r.meta || '{"rotation_root": true}'::jsonb
  FROM ref_tags rt
  JOIN tags t ON t.tag_id = rt.tag_id
 WHERE rt.ref_id = r.ref_id
   AND t.namespace = 'OPEN'
   AND t.value = 'level:strategic';

-- level:tactical -> meta.worker_mintable = false
UPDATE refs r
   SET meta = r.meta || '{"worker_mintable": false}'::jsonb
  FROM ref_tags rt
  JOIN tags t ON t.tag_id = rt.tag_id
 WHERE rt.ref_id = r.ref_id
   AND t.namespace = 'OPEN'
   AND t.value = 'level:tactical';

-- LLM:<value> -> meta.llm_tier = <value>
UPDATE refs r
   SET meta = r.meta || jsonb_build_object('llm_tier', t.value)
  FROM ref_tags rt
  JOIN tags t ON t.tag_id = rt.tag_id
 WHERE rt.ref_id = r.ref_id
   AND t.namespace = 'LLM';

-- level:proposed-tactical -> merge onto the plain `proposed-tactical`
-- open tag (0028's merge-then-delete pattern).
INSERT INTO tags (namespace, value)
SELECT 'OPEN', 'proposed-tactical'
WHERE EXISTS (
    SELECT 1 FROM tags WHERE namespace = 'OPEN' AND value = 'level:proposed-tactical'
)
ON CONFLICT (namespace, value) DO NOTHING;

UPDATE ref_tags rt
   SET tag_id = (
       SELECT tag_id FROM tags WHERE namespace = 'OPEN' AND value = 'proposed-tactical'
   )
 WHERE rt.tag_id = (
       SELECT tag_id FROM tags WHERE namespace = 'OPEN' AND value = 'level:proposed-tactical'
   )
   AND NOT EXISTS (
       SELECT 1 FROM ref_tags x
        WHERE x.ref_id = rt.ref_id
          AND x.tag_id = (
              SELECT tag_id FROM tags WHERE namespace = 'OPEN' AND value = 'proposed-tactical'
          )
   );

-- Drop every retired level:*/LLM:* tag row. ON DELETE CASCADE
-- (ref_tags / chunk_tags -> tags) sweeps up any leftover edges (e.g. a
-- ref that already carried both level:proposed-tactical and
-- proposed-tactical, skipped by the repoint above).
DELETE FROM tags
 WHERE (namespace = 'OPEN'
        AND value IN (
            'level:strategic', 'level:tactical', 'level:subtask',
            'level:recurring', 'level:proposed-tactical'
        ))
    OR namespace = 'LLM';

COMMIT;

-- End of 0102_todo_facet_normalize.sql
