-- 0220_drive_return_showcase.sql
--
-- docs/backlog/drive-filter-hierarchy.md slice 1: personal storage for
-- the Drive Return and Showcase tasks. Reto 2026-10-16: three tables in
-- one migration; Showcase private per user; latest-open only, no expiry.
--
--   * web_ref_views — one row per (user, ref): when that web user last
--     opened the ref in a full reader. Latest only — no click stream, no
--     URL or query text. Return's personal history.
--   * web_collections — one explicit curated collection per user
--     (named "Showcase"), created lazily on the first addition.
--   * web_collection_members — the refs a user added to their
--     collection; added_at is the first addition and never moves.
--
-- Keyed by web_users.id (the authenticated identity), never a login or
-- client-supplied id. Every FK cascades on delete: deleting the account
-- or hard-deleting a ref drops its rows; soft-retired refs keep theirs
-- (the store hides them) so an undelete restores history and membership.
-- No existing table changes and no backfill: the tables are born empty,
-- so the inline FKs validate instantly.
--
-- Forward-only (ADR 0005). Idempotent (IF NOT EXISTS throughout).
-- Regenerate the baseline snapshot after merge (ADR 0031).

BEGIN;

CREATE TABLE IF NOT EXISTS web_ref_views (
    user_id        bigint      NOT NULL REFERENCES web_users (id)
                               ON DELETE CASCADE,
    ref_id         bigint      NOT NULL REFERENCES refs (ref_id)
                               ON DELETE CASCADE,
    last_opened_at timestamptz NOT NULL,
    PRIMARY KEY (user_id, ref_id)
);

CREATE INDEX IF NOT EXISTS web_ref_views_user_recent_idx
    ON web_ref_views (user_id, last_opened_at DESC, ref_id DESC);
CREATE INDEX IF NOT EXISTS web_ref_views_ref_id_idx
    ON web_ref_views (ref_id);

COMMENT ON TABLE web_ref_views IS
    'Drive Return: when each web user last opened each ref in a full '
    'reader (drive-filter-hierarchy). One latest row per (user_id, '
    'ref_id), private to that user; no expiry.';

CREATE TABLE IF NOT EXISTS web_collections (
    collection_id bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_user_id bigint      NOT NULL UNIQUE REFERENCES web_users (id)
                              ON DELETE CASCADE,
    name          text        NOT NULL CHECK (name <> ''),
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE web_collections IS
    'Drive Showcase: one private curated collection per web user '
    '(drive-filter-hierarchy), created on the first explicit addition.';

CREATE TABLE IF NOT EXISTS web_collection_members (
    collection_id bigint      NOT NULL
                              REFERENCES web_collections (collection_id)
                              ON DELETE CASCADE,
    ref_id        bigint      NOT NULL REFERENCES refs (ref_id)
                              ON DELETE CASCADE,
    added_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (collection_id, ref_id)
);

CREATE INDEX IF NOT EXISTS web_collection_members_recent_idx
    ON web_collection_members (collection_id, added_at DESC, ref_id DESC);
CREATE INDEX IF NOT EXISTS web_collection_members_ref_id_idx
    ON web_collection_members (ref_id);

COMMENT ON TABLE web_collection_members IS
    'Drive Showcase membership: refs a user explicitly added to their '
    'collection. added_at is the first addition; a repeat add keeps it.';

COMMIT;

-- End of 0220_drive_return_showcase.sql
