-- 0350_drop_artifact_orphans.sql
--
-- Drop the schema-only orphan tables artifact_kinds and ref_artifacts
-- (gr690192). The derived-queue registry they were minted for became
-- self-contained pass closures with their own lease surfaces
-- (chunk_claims, app_state markers); no code reads or writes either.
BEGIN;

-- squawk-ignore ban-drop-table
DROP TABLE IF EXISTS ref_artifacts;
-- squawk-ignore ban-drop-table
DROP TABLE IF EXISTS artifact_kinds;

COMMIT;
