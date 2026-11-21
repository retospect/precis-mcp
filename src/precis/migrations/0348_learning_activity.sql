-- 0348_learning_activity.sql
BEGIN;

CREATE TABLE IF NOT EXISTS learning_activity (
    user_id bigint NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    source text NOT NULL CHECK (source IN ('anki_review', 'study_answer')),
    occurred_at timestamptz NOT NULL,
    event_key text NOT NULL,
    PRIMARY KEY (user_id, source, event_key)
);

CREATE INDEX IF NOT EXISTS learning_activity_owner_time_idx ON learning_activity (user_id, occurred_at);

ALTER TABLE learning_activity ENABLE ROW LEVEL SECURITY;
ALTER TABLE learning_activity FORCE ROW LEVEL SECURITY;
CREATE POLICY learning_activity_owner ON learning_activity
    USING (user_id = nullif(current_setting('precis.learning_user_id', true), '')::bigint)
    WITH CHECK (user_id = nullif(current_setting('precis.learning_user_id', true), '')::bigint);

COMMIT;
