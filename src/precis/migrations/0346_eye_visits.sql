-- 0346_eye_visits.sql
-- Per-login /eye walk records (gr743231): the owner's private navigation
-- trail through the graph. Handles and timestamps only — never query text
-- or rendered content. Owner-scoped reads in precis.eye_visits.
CREATE TABLE IF NOT EXISTS eye_visits (
    visit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    handle text NOT NULL CHECK (char_length(handle) BETWEEN 1 AND 200),
    kind text NOT NULL CHECK (char_length(kind) BETWEEN 1 AND 40),
    previous_handle text CHECK (
        previous_handle IS NULL
        OR char_length(previous_handle) BETWEEN 1 AND 200
    ),
    visited_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS eye_visits_user_time_idx
    ON eye_visits (user_id, visited_at DESC);
