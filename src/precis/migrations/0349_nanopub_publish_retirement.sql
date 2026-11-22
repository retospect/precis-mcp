-- 0349_nanopub_publish_retirement.sql
--
-- gr760607: a claim hub past 'candidate' (reviewed, signed, anchored,
-- published, or carrying a terminal publish row) retires only through the
-- retirement procedure, delete(kind='finding', ...,
-- args={'retire_published': True}). The procedure terminalizes the hub's
-- live publish row as 'superseded' (a successor hub is named) or
-- 'retracted' (none is), states the state machine already defines, and
-- keeps the row. These two columns carry what the state alone cannot:
-- why the hub was retired and which hub replaced it. Both stay NULL on
-- every row the procedure never touched, including rows terminalized by
-- the artifact's own supersede door (nanopub_supersessions names that
-- successor).
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE nanopub_publish ADD COLUMN IF NOT EXISTS retired_reason text;

-- The FK column is new and all NULL, so validation finds no row to check,
-- and nanopub_publish is small: the lock is brief.
-- squawk-ignore adding-foreign-key-constraint
ALTER TABLE nanopub_publish ADD COLUMN IF NOT EXISTS retired_successor_ref_id bigint REFERENCES refs(ref_id) ON DELETE SET NULL;

-- Covers the FK: a refs delete scans for referencing rows.
CREATE INDEX IF NOT EXISTS nanopub_publish_retired_successor_idx
    ON nanopub_publish (retired_successor_ref_id)
    WHERE retired_successor_ref_id IS NOT NULL;

COMMENT ON COLUMN nanopub_publish.retired_reason IS
    'gr760607: the reason the hub retirement procedure recorded when it '
    'retired this claim hub; NULL when the procedure never touched the row.';

COMMENT ON COLUMN nanopub_publish.retired_successor_ref_id IS
    'gr760607: the claim hub that superseded this one through the hub '
    'retirement procedure; NULL for a retraction or an untouched row.';

COMMIT;
