-- 0075_email_account.sql
--
-- The `email` kind's per-account registry (docs/design/email-kind.md, slice 1).
-- A live IMAP adapter browses a mailbox and (opt-in) promotes chosen messages
-- into the summarization pipeline; this row is the account's config + poll
-- high-water mark. The password / OAuth token itself lives in the secrets
-- vault (ADR 0055), NOT here — `secret_name` is only the vault key.
--
-- Neither existing config store fits: `service_config` (0072) is fixed-column
-- with no blob; `app_settings` (0070) is a string-scalar KV. So per-account
-- IMAP/SMTP settings that we don't query on (host/port/TLS/folders/cadence/
-- from-address/scan-policy/auth-mode) ride in a JSONB `config` bag; the
-- columns are only what the poll loop and CLI filter on.
--
-- `uidvalidity` guards `last_uid`: IMAP UIDs are only stable while UIDVALIDITY
-- is unchanged. If a poll sees a different UIDVALIDITY for a folder, the
-- high-water mark is stale and the folder must be resynced. (v1 watches a
-- single folder set; a later slice may split the high-water mark per folder.)
--
-- Forward-only (ADR 0005): additive, no data migration.

CREATE TABLE IF NOT EXISTS email_account (
    account      TEXT        PRIMARY KEY,          -- 'rs@retostamm.com'
    enabled      BOOLEAN     NOT NULL DEFAULT true,
    secret_name  TEXT        NOT NULL,             -- vault key for the password/token
    last_uid     BIGINT      NOT NULL DEFAULT 0,   -- poll high-water mark
    uidvalidity  BIGINT,                           -- guards last_uid; change ⇒ resync
    config       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE email_account IS
    'Per-account IMAP/SMTP registry for the email kind (secret in vault, not '
    'here); config JSONB carries imap/smtp/folders/poll_seconds/auth/scan_policy.';
