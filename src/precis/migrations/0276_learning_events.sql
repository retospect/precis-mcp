-- Private manual engagement records are separate from refs and mastery data.
-- Keep table creation and owner policies atomic if application is interrupted.
BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('learning', FALSE, 'Learning',
     'Private authenticated self-reported study engagement and preferences. '
     'No generic refs or mastery claims. See precis-learning-help.')
ON CONFLICT (slug) DO NOTHING;

CREATE TABLE IF NOT EXISTS learning_settings (
    user_id bigint PRIMARY KEY REFERENCES web_users(id) ON DELETE CASCADE,
    typical_session_minutes integer DEFAULT 30
        CHECK (typical_session_minutes BETWEEN 1 AND 1440),
    timezone text NOT NULL DEFAULT 'UTC',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS learning_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    study_date date NOT NULL,
    event_kind text NOT NULL DEFAULT 'checkin' CHECK (event_kind = 'checkin'),
    status text NOT NULL CHECK (status IN ('studied', 'rest')),
    minutes integer CHECK (minutes BETWEEN 0 AND 1440),
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 500),
    source text NOT NULL DEFAULT 'self_report' CHECK (source = 'self_report'),
    idempotency_key text NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 128),
    supersedes_id bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status <> 'rest' OR minutes IS NULL),
    UNIQUE (user_id, event_id),
    UNIQUE (user_id, idempotency_key),
    FOREIGN KEY (user_id, supersedes_id)
        REFERENCES learning_events(user_id, event_id)
);

CREATE INDEX IF NOT EXISTS learning_events_user_date_idx
    ON learning_events (user_id, study_date, event_id DESC);

CREATE INDEX IF NOT EXISTS learning_events_owner_supersedes_idx
    ON learning_events (user_id, supersedes_id);

ALTER TABLE learning_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE learning_settings FORCE ROW LEVEL SECURITY;
CREATE POLICY learning_settings_owner ON learning_settings
    USING (user_id = nullif(current_setting('precis.learning_user_id', true), '')::bigint)
    WITH CHECK (user_id = nullif(current_setting('precis.learning_user_id', true), '')::bigint);

ALTER TABLE learning_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE learning_events FORCE ROW LEVEL SECURITY;
CREATE POLICY learning_events_owner ON learning_events
    USING (user_id = nullif(current_setting('precis.learning_user_id', true), '')::bigint)
    WITH CHECK (user_id = nullif(current_setting('precis.learning_user_id', true), '')::bigint);

COMMIT;
