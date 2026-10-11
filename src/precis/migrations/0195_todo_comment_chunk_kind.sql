-- 0195_todo_comment_chunk_kind.sql
--
-- Register `chunk_kind='todo_comment'`: an append-only comment on a todo,
-- the `gripe_comment` shape (0005) applied to `kind='todo'`.
-- `put(kind='todo', id=N, text=...)` appends one; the todo's `todo_body`
-- chunk (0050) is untouched. is_card = FALSE ties the kind to ord >= 0 per
-- the chunks_check constraint.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('todo_comment', FALSE, 'Todo comment / append-only timeline entry')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0195_todo_comment_chunk_kind.sql
