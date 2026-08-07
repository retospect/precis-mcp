-- 0110_email_scan_attempt_lease.sql
--
-- `inject_scan` (slice 4) claims `email_scan` rows with `tier < 1` and
-- re-does the IMAP fetch + model call for each on every sweep. A model call
-- the local scorer can't parse (`parse_tier1_verdict` returns None) leaves
-- the row untouched -- no CAS write, no other marker -- so a persistently
-- unparseable message got re-fetched and re-scored every sweep, unbounded
-- (OPEN-ITEMS "Unbraked LLM-pass cluster").
--
-- `attempts` / `next_attempt_at` let the pass stamp a claim-time cooldown
-- immediately BEFORE the LLM call (mirroring `chunk_claims`' claim-before-
-- call ordering) so a raise or an unparseable reply still leaves the row
-- braked. Deliberately NOT applied to a bare IMAP fetch failure
-- (`ImapAuthError`/`OSError`) -- that path is an intentional "retry every
-- tick" transient-network design already (see `inject_scan.py`), unchanged
-- here.
--
-- Both columns default to their empty state, so an untouched table is
-- functionally identical to today (`next_attempt_at IS NULL` never excludes
-- a row).
--
-- Forward-only (ADR 0005): additive, no data migration.

ALTER TABLE email_scan ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE email_scan ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;

COMMENT ON COLUMN email_scan.attempts IS
    'Count of inject_scan model-call attempts against this row (any tier<1 row); bumped at claim time, before the call.';
COMMENT ON COLUMN email_scan.next_attempt_at IS
    'Claim-time cooldown -- pending_email_scans excludes a row while this is in the future; NULL = eligible now.';
