-- 0215_trust_constraints_validate.sql
--
-- Validate the NOT VALID trust CHECKs 0214 added (graph-node-trust-state
-- slice 1). A separate file is a separate migration transaction: VALIDATE
-- takes SHARE UPDATE EXCLUSIVE, which does not block reads or writes, and
-- 0214's ACCESS EXCLUSIVE locks are already released. Every existing row
-- holds the constant defaults (unknown/unknown/0), so the scans cannot fail.
--
-- Forward-only (ADR 0005).

BEGIN;

ALTER TABLE refs VALIDATE CONSTRAINT refs_trust_state_check;
ALTER TABLE links VALIDATE CONSTRAINT links_trust_state_check;
ALTER TABLE revisions VALIDATE CONSTRAINT revisions_event_check;
ALTER TABLE revisions VALIDATE CONSTRAINT revisions_trust_acts_array;

COMMIT;
