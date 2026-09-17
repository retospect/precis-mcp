-- 0166_refs_owner_login_validate.sql
--
-- (0165 is taken by a sibling tree's unshipped migration.) Validate the ``refs.owner_login`` foreign key that 0164 added
-- ``NOT VALID``. A separate file because the migration runner applies
-- each file in its own transaction: ``VALIDATE CONSTRAINT`` scans
-- ``refs`` under SHARE UPDATE EXCLUSIVE (writers keep going), which only
-- helps once 0164's SHARE ROW EXCLUSIVE lock has been released at its
-- commit. The column is NULL on every row at this point, so validation
-- cannot fail.
--
-- Forward-only (ADR 0005). Idempotent: validating an already-valid
-- constraint is a no-op.

BEGIN;

ALTER TABLE refs VALIDATE CONSTRAINT refs_owner_login_fkey;

COMMIT;

-- End of 0166_refs_owner_login_validate.sql
