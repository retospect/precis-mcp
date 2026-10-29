-- 0263_paper_note_check_validate.sql
--
-- Validate the NOT VALID chunks_check 0262 rewrote to admit
-- chunk_kind='paper_note' in the ord < 0 band (gr521094). A separate
-- file is a separate migration transaction: VALIDATE takes SHARE
-- UPDATE EXCLUSIVE, which does not block reads or writes, and 0262's
-- ACCESS EXCLUSIVE locks are already released. Every existing row
-- satisfies the widened check (ord < 0 rows are all card_%; ord >= 0
-- rows are unchanged for every other kind), so the scan cannot fail.
--
-- Forward-only (ADR 0005).

BEGIN;

ALTER TABLE chunks VALIDATE CONSTRAINT chunks_check;

COMMIT;

-- End of 0263_paper_note_check_validate.sql
