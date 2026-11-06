-- 0287_ref_tags_principal.sql
--
-- Write principal on tag rows (td575705 G1, gr575691). Every write now
-- records its server-set principal — slice:<grant>:<role>, install,
-- user:<abbrev>, personal:<id>, worker:<source> — in chunk
-- meta.principal, filed_by.principal, revisions.writer.principal and,
-- here, ref_tags.principal. ``set_by`` is an FK to the closed ``actors``
-- table (migration 0010 CHECK list) and cannot carry these strings, so
-- the principal gets its own nullable column.
--
--   * ref_tags.principal: text, NULL on pre-G1 rows and on tag writes
--     that bypassed the MCP code path (direct SQL, other writers) —
--     never backfilled by guess. G2 reads NULL as unknown, never as
--     trusted.
--   * Tag removal deletes the row; there is no tombstone to attribute.
--   * chunk_tags keeps no column: the G2 trust question is "which
--     principal set this ref's STATUS/tag", a ref-level query.
--
-- Forward-only (ADR 0005). Metadata-only ADD COLUMN; the wait on
-- ref_tags is bounded so a busy table fails the apply rather than
-- queueing writers. Idempotent.

BEGIN;

SET LOCAL lock_timeout = '10s';

ALTER TABLE ref_tags ADD COLUMN IF NOT EXISTS principal text;

COMMENT ON COLUMN ref_tags.principal IS
    'Server-set write principal (td575705 G1): slice:<grant>:<role>, '
    'install, user:<abbrev>, personal:<token id> or worker:<source>. '
    'NULL means pre-G1 or a non-MCP writer — G2 reads it as unknown, '
    'never as trusted. set_by stays the closed actors enum; this is '
    'the column factory readers check before trusting a STATUS/tag.';

COMMIT;
