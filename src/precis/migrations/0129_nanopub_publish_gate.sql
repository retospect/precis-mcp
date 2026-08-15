-- 0129_nanopub_publish_gate.sql
--
-- Publication-time trust gate + publish bookkeeping (slice 4/5 of
-- docs/backlog/claim-publication-nanopub-ots.md):
--
--   * nanopub_trust_allowlist — the explicit (identity, key-fingerprint)
--     allowlist consulted at publish time. Pin KEYS, not bare identities
--     (a new key on an allowlisted identity is not auto-trusted); flat,
--     zero transitivity by design (npx:approvesOf may inform adding an
--     entry by hand, never automatically). `attesting` marks the one
--     human key whose signature means "a human checked" — a bot entry is
--     explicitly non-attesting and its signature alone publishes
--     nothing. Validity windows (`valid_from`/`valid_until`) are stored
--     now; window-vs-signature-time enforcement is deferred until the
--     OTS anchor supplies trustworthy signature time (spec: Publish-time
--     gates #4). Publishing the allowlist itself as a signed, anchored
--     artifact is likewise deferred (#3) — this table is the working
--     copy that artifact would snapshot.
--
--   * nanopub_publish.published_at / registry_url — set once by the
--     registry POST (slice 5, the one true point of no return); the
--     state machine row flips `anchored` → `published` in the same
--     transaction.
--
-- Forward-only (ADR 0005): additive, no data migration.

BEGIN;

CREATE TABLE IF NOT EXISTS nanopub_trust_allowlist (
    id              BIGSERIAL   PRIMARY KEY,
    -- ORCID URI for a human; the precis identity URI for the bot.
    identity_uri    TEXT        NOT NULL,
    -- sha256 hex of the DER public key (precis.nanopub.keys.fingerprint).
    key_fingerprint TEXT        NOT NULL,
    attesting       BOOLEAN     NOT NULL DEFAULT FALSE,
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until     TIMESTAMPTZ,
    note            TEXT        NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (identity_uri, key_fingerprint)
);

COMMENT ON TABLE nanopub_trust_allowlist IS
    'Publication-time trust gate: only signatures whose (identity, key '
    'fingerprint) pair is listed here are trusted at publish; attesting=TRUE '
    'marks the human key ("only human-attested claims are publishable"). '
    'Flat, zero transitivity; keys pinned, never bare identities '
    '(docs/backlog/claim-publication-nanopub-ots.md).';

ALTER TABLE nanopub_publish
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS registry_url TEXT;

COMMIT;
