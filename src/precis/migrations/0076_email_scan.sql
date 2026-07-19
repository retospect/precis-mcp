-- 0076_email_scan.sql
--
-- Slice 3 of the email kind (docs/design/email-kind.md): the `mail_poll`
-- compute pass walks each account's primary watched folder for messages past
-- the `last_uid` high-water, runs a tier-0 regex injection scan inline, and
-- records one verdict row per message here.
--
-- The message BODY is never stored — IMAP stays the source of truth (the
-- "don't mirror the mailbox" invariant). Only the scan verdict + evidence
-- persist, so the browse handler can badge and the slice-4 `inject_scan` pass
-- can lease + escalate the ambiguous ones (re-fetching the body from IMAP).
--
-- Keyed by (account, folder, uidvalidity, uid): IMAP UIDs are only stable
-- while UIDVALIDITY is unchanged, so it is part of the identity — a folder
-- resync (new UIDVALIDITY) yields fresh rows rather than colliding with stale
-- ones. `tier` is the highest scan tier that produced the verdict (0 = the
-- mail_poll regex); the partial index is the slice-4 lease of not-yet-deep
-- messages. `evidence` carries which signals fired + the scanner version so
-- false positives are tunable and re-scans are a version bump.
--
-- The three ALTERs give `email_account` its poll bookkeeping: the pass paces
-- itself off `last_polled_at` (per-account cadence from config.poll_seconds)
-- and backs off exponentially on `consecutive_errors`, exactly like
-- news_sources / fetch / chase.
--
-- Forward-only (ADR 0005): additive, no data migration.

CREATE TABLE IF NOT EXISTS email_scan (
    account      TEXT        NOT NULL,
    folder       TEXT        NOT NULL,
    uidvalidity  BIGINT      NOT NULL,
    uid          BIGINT      NOT NULL,
    verdict      TEXT        NOT NULL,        -- clean | suspect | high
    tier         SMALLINT    NOT NULL,        -- highest scan tier applied (0 = regex)
    evidence     JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- {signals:[...], version:N}
    scanned_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, folder, uidvalidity, uid)
);

COMMENT ON TABLE email_scan IS
    'Per-message injection-scan verdict for the email kind (no body stored); '
    'keyed by (account,folder,uidvalidity,uid). tier 0 = mail_poll regex.';

-- The slice-4 lease target: messages a deeper (tier >= 1) scan hasn't reached.
CREATE INDEX IF NOT EXISTS email_scan_pending_idx
    ON email_scan (account, tier) WHERE tier < 1;

ALTER TABLE email_account
    ADD COLUMN IF NOT EXISTS last_polled_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS consecutive_errors INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_status        TEXT;
