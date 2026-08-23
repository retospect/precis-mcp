-- 0134_web_users_orcid.sql
--
-- The account's ORCID iD. Nanopub signatures are attributed to an ORCID, not
-- to a login: `precis.nanopub.keys.load_profile` builds the signing profile
-- around `NANOPUB_ATTESTING_ORCID`, and the artifact row stores that URI as
-- its `signer`. Until now nothing connected the human sitting at /account to
-- that identity — the box knew who it signed as, but not who that was.
--
-- Nullable, and deliberately not unique-by-default... it IS unique: two
-- accounts claiming the same iD is either a duplicate person or an error, and
-- a partial unique index says so without forcing anyone to have one.
--
-- Stored in the canonical dashed 16-character form
-- (`precis.ingest.orcid.normalize_orcid_id` — one checksum implementation,
-- shared with the `orcid` kind), never the URL form: the https:// prefix is
-- rendering, and storing it would make "is this the same iD" a string-shape
-- question. The CHECK is shape-only; the ISO 7064 checksum is enforced in
-- Python at every write door (`precis.users.normalize_orcid`).
--
-- Forward-only (ADR 0005). Idempotent.

ALTER TABLE web_users
    ADD COLUMN IF NOT EXISTS orcid text
    CHECK (orcid IS NULL OR orcid ~ '^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]$');

CREATE UNIQUE INDEX IF NOT EXISTS web_users_orcid_key
    ON web_users (orcid) WHERE orcid IS NOT NULL;

COMMENT ON COLUMN web_users.orcid IS
    'Canonical dashed ORCID iD. The identity a nanopub this person signs is attributed to.';
