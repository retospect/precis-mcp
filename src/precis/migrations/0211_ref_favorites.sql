-- 0211_ref_favorites.sql
--
-- Per-user favorites for the precis-web UI (gr486409): the heart toggle
-- on the graph focus page and the ref/paper readers, and the
-- /favorites list they feed. Until now there was no per-human pref
-- store at all — tags are global (a FAV:<login> tag would leak who
-- favorited what into tag search and autocomplete), and refs.meta is
-- one shared row, not per user.
--
-- One row = "<login> hearted <ref_id>". Private by construction: only
-- the owning login's rows are ever read by the routes; nothing renders
-- another user's set.
--
--   * login   — FK to web_users.login. CASCADE both ways: a login
--     rename keeps the rows, deleting the account drops them (the prefs
--     are meaningless without their owner).
--   * ref_id  — FK to refs. CASCADE on delete: refs are only ever
--     soft-deleted (retired_at) in practice, so a hearted ref survives
--     as a tombstone on the favorites list; the cascade is the
--     correctness floor if a row ever does get hard-deleted.
--   * created_at — when the heart was set; the list orders newest
--     first on it.
--
-- PRIMARY KEY (login, ref_id) makes the toggle idempotent (INSERT … ON
-- CONFLICT DO NOTHING / DELETE) and covers the login-FK cascade lookup.
-- The separate ref_id index covers the refs-FK cascade lookup the
-- schema test insists on.
--
-- The FKs are inline, not NOT VALID (contrast 0164): the table is born
-- empty, so the validating scan is instant and the lock window is nil.
--
-- Forward-only (ADR 0005). Idempotent (IF NOT EXISTS throughout).

BEGIN;

CREATE TABLE IF NOT EXISTS ref_favorites (
    login      text        NOT NULL REFERENCES web_users (login)
                           ON UPDATE CASCADE ON DELETE CASCADE,
    ref_id     bigint      NOT NULL REFERENCES refs (ref_id)
                           ON UPDATE CASCADE ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (login, ref_id)
);

CREATE INDEX IF NOT EXISTS ref_favorites_ref_id_idx
    ON ref_favorites (ref_id);

COMMENT ON TABLE ref_favorites IS
    'Per-web-user favorites (hearts) for the precis-web UI — one row per '
    '(login, ref_id), private to that login. Soft-deleted refs stay as '
    'tombstone rows; created_at is when the heart was set.';

COMMIT;

-- End of 0211_ref_favorites.sql
