-- 0362_drive_attribution.sql
-- Existing opens have no reliable actor. Keep them independently of new opens.
BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE web_ref_views
    ADD COLUMN IF NOT EXISTS attribution text NOT NULL DEFAULT 'unknown'
        CHECK (attribution IN ('human', 'agent', 'unknown'));

COMMENT ON TABLE web_ref_views IS
    'Drive Return: latest open per account, ref, and attribution; no expiry.';

-- Reopening as one actor must keep the other actor's timestamp.
CREATE UNIQUE INDEX IF NOT EXISTS web_ref_views_attribution_key
    ON web_ref_views (user_id, ref_id, attribution);
-- squawk-ignore ban-drop-constraint
ALTER TABLE web_ref_views DROP CONSTRAINT IF EXISTS web_ref_views_pkey;
ALTER TABLE web_ref_views ADD CONSTRAINT web_ref_views_pkey
    PRIMARY KEY USING INDEX web_ref_views_attribution_key;

CREATE INDEX IF NOT EXISTS web_ref_views_attribution_recent_idx
    ON web_ref_views (user_id, attribution, last_opened_at DESC, ref_id DESC);

CREATE TABLE IF NOT EXISTS web_ref_creations (
    user_id bigint NOT NULL REFERENCES web_users (id) ON DELETE CASCADE,
    ref_id bigint PRIMARY KEY REFERENCES refs (ref_id) ON DELETE CASCADE,
    attribution text NOT NULL CHECK (attribution IN ('human', 'agent'))
);

CREATE INDEX IF NOT EXISTS web_ref_creations_user_idx
    ON web_ref_creations (user_id, attribution, ref_id DESC);

COMMENT ON TABLE web_ref_creations IS
    'Drive creation attribution under an authenticated account; no access grants.';

COMMIT;
