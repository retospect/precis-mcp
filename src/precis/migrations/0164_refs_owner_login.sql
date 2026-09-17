-- 0164_refs_owner_login.sql
--
-- Per-record ownership for refs whose kind is inherently per-user.
-- ``refs`` has no notion of "this row belongs to a human" today —
-- ownership-shaped needs have gone through ``meta`` keys, which gives
-- no FK integrity and no indexable filter. ``owner_login`` is a real
-- column instead: a nullable FK to ``web_users.login``, NULL meaning
-- "unowned" (the common case for every kind but the ones that opt in).
--
-- First consumer: the ``anki`` kind. Each anki card belongs to the
-- AnkiWeb account it syncs to, so ``owner_login`` is how the anki
-- worker scopes a sync run to one user's cards and how the web UI
-- filters "my cards" without a meta-key convention every reader has to
-- learn. ``ON DELETE SET NULL`` — deleting a web_users row must not
-- cascade into deleting the refs it owned, it just orphans them back to
-- unowned; ``ON UPDATE CASCADE`` keeps a login rename (if that ever
-- happens) from silently orphaning every row it owned.
--
-- No data backfill here and no literal logins — this is a public repo,
-- and the anki worker claims already-unowned rows for its account at
-- runtime (``claim_unowned_refs``), not at migration time.
--
-- The FK is added ``NOT VALID`` here and validated by 0166 in its own
-- transaction: an inline ``REFERENCES`` takes a SHARE ROW EXCLUSIVE lock
-- on both ``refs`` and ``web_users`` for the duration of a full scan of
-- ``refs`` (~10^5 rows on prod, hot for writes). ``NOT VALID`` makes the
-- add instant. The runner (``store/migrate.py``) wraps each FILE in one
-- transaction, so the validating scan has to live in a separate file to
-- run after this lock is released.
--
-- Forward-only (ADR 0005). Idempotent (``IF NOT EXISTS`` throughout; the
-- constraint add is guarded by a catalog check).

BEGIN;

ALTER TABLE refs ADD COLUMN IF NOT EXISTS owner_login text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'refs_owner_login_fkey'
    ) THEN
        ALTER TABLE refs
            ADD CONSTRAINT refs_owner_login_fkey
            FOREIGN KEY (owner_login) REFERENCES web_users (login)
            ON UPDATE CASCADE ON DELETE SET NULL
            NOT VALID;
    END IF;
END
$$;

-- Two partial indexes: ``(owner_login)`` covers the FK's cascade lookup
-- (``ON DELETE SET NULL`` / ``ON UPDATE CASCADE`` seq-scan ``refs``
-- otherwise — the covering-index schema test insists on a leading-column
-- match), ``(kind, owner_login)`` serves the per-kind "this user's cards"
-- filter the anki worker runs every tick.
CREATE INDEX IF NOT EXISTS refs_owner_login_idx
    ON refs (owner_login)
    WHERE owner_login IS NOT NULL;

CREATE INDEX IF NOT EXISTS refs_kind_owner_idx
    ON refs (kind, owner_login)
    WHERE owner_login IS NOT NULL;

COMMENT ON COLUMN refs.owner_login IS
    'FK to web_users.login — the human this ref belongs to. NULL means '
    'unowned (every kind but the per-user ones, e.g. anki, default to '
    'this). ON DELETE SET NULL: removing the web user orphans the row '
    'rather than deleting it.';

COMMIT;

-- End of 0164_refs_owner_login.sql
