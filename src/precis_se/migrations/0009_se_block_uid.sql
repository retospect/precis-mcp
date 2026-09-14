-- precis_se/0009_se_block_uid.sql
--
-- Stable block identity for `se` (docs/backlog/design-state-core.md item 2,
-- the plugin half of core migration `0162_design_core.sql`): every block row
-- carries a `uid` minted once from `design_block_uid_seq` and CARRIED
-- FORWARD across the retire-all/reinsert-all save, and every in-design
-- cross-reference gains a uid column beside its name text.
--
-- WHY, in one line: `se_blocks.id` is rebuilt on every save (`save_tree`
-- retires the whole design and reinserts it), so a row id can never be an
-- external identity — not a viewer path leaf, not a cache key, not the key a
-- per-block state or a provenance sidecar hangs off. The uid is that
-- identity instead; core's `design_states`/`design_block_state`/
-- `design_branches` are already keyed `(ref_id, block_uid)` waiting for it.
--
-- WHY THE NAME COLUMNS STAY. Two reasons, both load-bearing:
--
--   * a cross-reference may legally DANGLE (a measure on a block that was
--     removed, a BOM line naming a block not yet added, a threading
--     invariant whose object went away) — those are read-time DRC findings,
--     never write-time rejections, so the uid column is NULLABLE and the
--     name text is what the finding can still print;
--   * a name is now a *display label*: unique per live design (the
--     `se_blocks_ref_name_key` index STANDS — design-state-core.md item 2
--     keeps label uniqueness for now), resolved to a uid at write time and
--     rendered back from the uid at read time.
--
-- The uid column is therefore the authoritative join at load; the name is
-- the fallback for exactly the dangling case, and what humans read.
--
-- WHY `(ref_id, uid)` AND NOT A GLOBAL UNIQUE. A branch is a naive full
-- copy of a design with its uids PRESERVED (design-state-core.md item 5 —
-- that is what makes a branch diffable block-by-block), so two live designs
-- legitimately share a uid. Uniqueness is per design, matching core's
-- `(ref_id, block_uid)` keying.
--
-- `template_uid` covers a LOCAL template reference only. A cross-design
-- reference (`'design-slug#block-name'`) stays name text here because its
-- resolution lives in `precis.blocktree.ops.resolve_template`, which walks
-- foreign *trees* by slug and has no uid→design index to walk instead;
-- converting it is its own slice (design-state-core.md §Target + blast
-- radius names `precis.blocktree` template_ref resolution separately).
--
-- Backfill mints one uid per distinct `(ref_id, name)` and stamps it on the
-- live row AND on every retired row of that same block, so a design's
-- history keeps one identity per block rather than one per save.
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration
-- (namespace `precis_se`), applied after 0001-0008.

BEGIN;

-- 1. the identity column ---------------------------------------------------
ALTER TABLE se_blocks ADD COLUMN IF NOT EXISTS uid bigint;
ALTER TABLE se_blocks ADD COLUMN IF NOT EXISTS template_uid bigint;

-- One uid per (ref_id, name) — nextval is evaluated once per row of the
-- DISTINCT set, then fanned out over every (live and retired) row of that
-- block.
WITH minted AS (
    SELECT ref_id, name, nextval('design_block_uid_seq') AS uid
    FROM (SELECT DISTINCT ref_id, name FROM se_blocks WHERE uid IS NULL) d
)
UPDATE se_blocks b
   SET uid = minted.uid
  FROM minted
 WHERE b.ref_id = minted.ref_id
   AND b.name = minted.name
   AND b.uid IS NULL;

ALTER TABLE se_blocks ALTER COLUMN uid SET NOT NULL;

COMMENT ON COLUMN se_blocks.uid IS
    'Stable block identity, minted once from design_block_uid_seq and '
    'carried forward by persist.save_tree across the retire-all/reinsert-all '
    'cycle that rebuilds this row''s id. THE external identity: row ids are '
    'ephemeral and never exposed. Preserved by branch copies, so two live '
    'designs may share a uid — uniqueness is per (ref_id, uid).';
COMMENT ON COLUMN se_blocks.template_uid IS
    'The uid of a LOCAL template reference (template_ref without a '
    '''slug#'' qualifier); NULL for a cross-design reference, whose '
    'resolution still goes through the name text in template_ref.';

-- Unique per design over live rows only: retired rows of the same block
-- share its uid by design (that is the history), and a branch copy shares it
-- across designs.
CREATE UNIQUE INDEX IF NOT EXISTS se_blocks_ref_uid_key
    ON se_blocks (ref_id, uid) WHERE retired_at IS NULL;
-- Plain (non-partial): the lookup "which block is uid N" must find a retired
-- row too, so a stale reference resolves to "that block, now gone" rather
-- than to nothing at all.
CREATE INDEX IF NOT EXISTS se_blocks_uid_idx ON se_blocks (uid);

-- 2. cross-references gain their uid half ----------------------------------
ALTER TABLE se_connects ADD COLUMN IF NOT EXISTS a_block_uid bigint;
ALTER TABLE se_connects ADD COLUMN IF NOT EXISTS b_block_uid bigint;
ALTER TABLE se_measures ADD COLUMN IF NOT EXISTS block_uid bigint;
ALTER TABLE se_bom      ADD COLUMN IF NOT EXISTS block_uid bigint;
ALTER TABLE se_bom      ADD COLUMN IF NOT EXISTS a_block_uid bigint;
ALTER TABLE se_bom      ADD COLUMN IF NOT EXISTS b_block_uid bigint;
ALTER TABLE se_topology ADD COLUMN IF NOT EXISTS subject_uid bigint;
ALTER TABLE se_topology ADD COLUMN IF NOT EXISTS object_uid bigint;

COMMENT ON COLUMN se_connects.a_block_uid IS
    'Endpoint identity (se_blocks.uid). NULL only when the endpoint name '
    'does not resolve in the design — a dangling reference is a read-time '
    'DRC finding, never a write-time rejection, so the name column stays as '
    'the display label and the finding''s subject.';
COMMENT ON COLUMN se_topology.subject_uid IS
    'Identity half of subject_name (se_blocks.uid); NULL when the name does '
    'not resolve — see se_connects.a_block_uid.';

-- Backfill every cross-reference from the (ref_id, name) map the block
-- backfill above just established — one `WITH` per statement rather than a
-- temp table, so this file stays runnable statement-by-statement (the
-- plugin-migration test fixtures strip BEGIN/COMMIT and execute the body
-- whole). DISTINCT because a block has one row per save and they all share
-- the uid.
WITH m AS (SELECT DISTINCT ref_id, name, uid FROM se_blocks)
UPDATE se_blocks b SET template_uid = m.uid
  FROM m
 WHERE b.template_uid IS NULL
   AND b.template_ref IS NOT NULL
   AND position('#' in b.template_ref) = 0
   AND m.ref_id = b.ref_id AND m.name = b.template_ref;

WITH m AS (SELECT DISTINCT ref_id, name, uid FROM se_blocks)
UPDATE se_connects c SET a_block_uid = m.uid
  FROM m
 WHERE c.a_block_uid IS NULL AND m.ref_id = c.ref_id AND m.name = c.a_block;
WITH m AS (SELECT DISTINCT ref_id, name, uid FROM se_blocks)
UPDATE se_connects c SET b_block_uid = m.uid
  FROM m
 WHERE c.b_block_uid IS NULL AND m.ref_id = c.ref_id AND m.name = c.b_block;

WITH m AS (SELECT DISTINCT ref_id, name, uid FROM se_blocks)
UPDATE se_measures s SET block_uid = m.uid
  FROM m
 WHERE s.block_uid IS NULL AND m.ref_id = s.ref_id AND m.name = s.block;

WITH m AS (SELECT DISTINCT ref_id, name, uid FROM se_blocks)
UPDATE se_bom s SET block_uid = m.uid
  FROM m
 WHERE s.block_uid IS NULL AND m.ref_id = s.ref_id AND m.name = s.block;
WITH m AS (SELECT DISTINCT ref_id, name, uid FROM se_blocks)
UPDATE se_bom s SET a_block_uid = m.uid
  FROM m
 WHERE s.a_block_uid IS NULL AND m.ref_id = s.ref_id AND m.name = s.a_block;
WITH m AS (SELECT DISTINCT ref_id, name, uid FROM se_blocks)
UPDATE se_bom s SET b_block_uid = m.uid
  FROM m
 WHERE s.b_block_uid IS NULL AND m.ref_id = s.ref_id AND m.name = s.b_block;

WITH m AS (SELECT DISTINCT ref_id, name, uid FROM se_blocks)
UPDATE se_topology t SET subject_uid = m.uid
  FROM m
 WHERE t.subject_uid IS NULL AND m.ref_id = t.ref_id AND m.name = t.subject_name;
WITH m AS (SELECT DISTINCT ref_id, name, uid FROM se_blocks)
UPDATE se_topology t SET object_uid = m.uid
  FROM m
 WHERE t.object_uid IS NULL AND m.ref_id = t.ref_id AND m.name = t.object_name;

COMMIT;

-- End of 0009_se_block_uid.sql
