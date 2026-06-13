-- 0013_todo_tree.sql
--
-- Hierarchical task graph for kind='todo'. A new `parent_id` column
-- on refs lets a todo point at its parent todo, forming a tree:
-- strategic roots → tactical branches → subtask leaves. Branches are
-- outcomes ("what does done look like"); leaves are next physical
-- actions (Allen-style GTD). See docs/design/todo-tree-plan.md for
-- the full design.
--
-- Why on refs and not on a kind-specific table: parent_id is a
-- generic "this ref points at that ref" relation. Today only `todo`
-- uses it, but a future kind that wants hierarchy (e.g. multi-file
-- book projects) can reuse the same column without another
-- migration. The partial index keeps the unindexed-NULL refs
-- (~all of them today) cheap.
--
-- Storage decisions documented in the plan:
-- - level (strategic|tactical|subtask) → tag `level:<tier>`
-- - claim → tag `claimed-by:<handle>` (decision #2 in plan)
-- - blocked-by → existing links table with relation='blocks'
-- - note-for (worker breadcrumbs) → existing links, relation='note-for'
-- - auto-check → JSONB in refs.meta (no schema; existing column)
--
-- No other schema changes. Everything else is tag-driven or links-
-- driven. New canonical STATUS: values (paused, auto-timeout) are
-- handler-level vocabulary, not enforced at the DB layer.
--
-- Forward-only (ADR 0005). Additive; existing todos behave exactly
-- as before (parent_id NULL = orphan, doesn't appear in tree views).

BEGIN;

ALTER TABLE refs
    ADD COLUMN IF NOT EXISTS parent_id BIGINT NULL
        REFERENCES refs(ref_id) ON DELETE SET NULL;

-- Partial index — only refs that participate in a tree get indexed.
-- The vast majority of refs (papers, memories, etc.) have
-- parent_id NULL and pay no index-maintenance cost.
CREATE INDEX IF NOT EXISTS refs_parent_id_idx
    ON refs (parent_id)
    WHERE parent_id IS NOT NULL;

COMMIT;

-- End of 0013_todo_tree.sql
