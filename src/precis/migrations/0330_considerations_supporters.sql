-- 0330_considerations_supporters.sql
--
-- review td688001 (gr626718): the finding-hub mint's converge path writes
-- each run's complete evidence association onto its `considered`
-- observation edge as meta.supporters — every resolved supporter's
-- identity, grounded `pc` pins and ref-level source handles — and the
-- write door replaces that block correctly on re-judgment. But the
-- append-only `considerations` history row never recorded it: the table
-- had no column, so the INSERT dropped it and the merge-resolved edge
-- rebuild (which strips judgment keys and re-stamps them from the latest
-- judgment row) could not restore it — a re-judged or merged pair
-- irrecoverably lost the earlier run's ref-level evidence association,
-- the fix-forward audit gr626718 exists to keep.
--
-- `supporters` mirrors `passages`: a jsonb list of evidence-source
-- identities, '[]' for a judgment that recorded none. Rows predating the
-- column default to '[]' — their supporters were never recorded, an
-- honest absence rather than a guess.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE considerations
    ADD COLUMN IF NOT EXISTS supporters jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN considerations.supporters IS
    'td688001 (gr626718): the judgment''s complete evidence association '
    '— every resolved supporter''s identity (grounded pc pins and '
    'ref-level source handles), parallel to passages. ''[]'' = none '
    'recorded (incl. pre-0330 rows).';

COMMIT;

-- End of 0330_considerations_supporters.sql
