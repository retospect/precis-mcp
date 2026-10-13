-- 0202_cache_state_content_sha256.sql
--
-- The web anchor for nanopub evidence (Reto 2026-10-03, review item
-- claims-and-evidence-3, option 1): a web page is mutable, so a nanopub
-- that quotes one cites it by the sha256 of what was fetched
-- (`urn:sha256:<sha>` + the URL as a second triple), the passage's sha is
-- frozen at approve, and the claim page shows "source changed since
-- approval" when a later fetch's sha differs.
--
--   * `cache_state.content_sha256` — sha256 (hex) of the raw response bytes
--     of the LAST fetch; every fetch/refresh overwrites it. Nullable and
--     NOT backfilled: a page cached before this migration gets a sha at
--     its next refresh, and until then the mint gate refuses it as a
--     source with "re-fetch the page". Only `kind='web'` writes it today;
--     other cache providers leave it NULL.
--
-- Forward-only (ADR 0005). Idempotent: ADD COLUMN IF NOT EXISTS. A
-- nullable column with no default is a metadata-only change.

BEGIN;

ALTER TABLE cache_state
    ADD COLUMN IF NOT EXISTS content_sha256 text;

COMMENT ON COLUMN cache_state.content_sha256 IS
    'sha256 (hex) of the raw response bytes of the last fetch (web only); overwritten by every refresh; NULL until a page is re-fetched after migration 0202.';

COMMIT;

-- End of 0202_cache_state_content_sha256.sql
