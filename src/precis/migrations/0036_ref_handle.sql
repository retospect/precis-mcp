-- 0036_ref_handle.sql
--
-- ADR 0036 — universal handles. The record-level handle column.
--
-- Every persistent ref gets one flat, type-prefixed Crockford-base32
-- handle (e.g. `pa4m8p1rz`), minted at insert (store.create_ref), the
-- single address form. `chunks.handle` already exists (ADR 0033, for
-- draft chunks, base-58); unifying chunk handles onto this scheme is a
-- later slice — this migration is records only.
--
-- Additive, nullable, NO backfill, so existing behaviour is preserved:
-- nothing reads `handle` except `resolve_handle`, refs minted before
-- this stay NULL until re-minted, and the surface only infers a kind
-- from an id that is a *well-formed, resolvable* handle (legacy slugs /
-- numerics fall through untouched).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE refs ADD COLUMN IF NOT EXISTS handle TEXT;

-- Global uniqueness across all minted record handles. Partial, so the
-- many legacy NULLs never collide and the index stays small until the
-- corpus is re-minted.
CREATE UNIQUE INDEX IF NOT EXISTS refs_handle_key
    ON refs (handle) WHERE handle IS NOT NULL;

COMMIT;

-- End of 0036_ref_handle.sql
