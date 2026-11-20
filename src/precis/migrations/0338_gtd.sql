-- 0338_gtd.sql
--
-- Private gtd items, change log and settings live in gtd-only tables keyed on
-- web_users.id under forced row-level security. No shared refs, tags, links,
-- chunks or embeddings. Every later gtd column is declared here: sealed.
BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('gtd', FALSE, 'Getting things done',
     'Private authenticated inbox, actions and projects. '
     'No generic refs, tags or links.')
ON CONFLICT (slug) DO NOTHING;

CREATE TABLE IF NOT EXISTS gtd_items (
    item_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    list text NOT NULL DEFAULT 'inbox'
        CHECK (list IN ('inbox', 'action', 'project', 'waiting', 'someday')),
    state text NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'done', 'dropped')),
    text text NOT NULL,
    captured text NOT NULL,
    capture_uncertain boolean NOT NULL DEFAULT false,
    note text NOT NULL DEFAULT '',
    context text,
    person text,
    project_id bigint,
    todo_ref bigint,
    surface_on date,
    follow_up_on date,
    due_on date,
    repeat text,
    source text NOT NULL DEFAULT 'chat',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    search_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', text || ' ' || note || ' ' || captured)
    ) STORED,
    UNIQUE (user_id, item_id),
    FOREIGN KEY (user_id, project_id)
        REFERENCES gtd_items(user_id, item_id)
);

CREATE INDEX IF NOT EXISTS gtd_items_user_list_state_idx
    ON gtd_items (user_id, list, state);

CREATE INDEX IF NOT EXISTS gtd_items_user_project_idx
    ON gtd_items (user_id, project_id);

CREATE INDEX IF NOT EXISTS gtd_items_search_idx
    ON gtd_items USING gin (search_tsv);

CREATE TABLE IF NOT EXISTS gtd_item_log (
    log_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    item_id bigint NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    before jsonb,
    after jsonb,
    FOREIGN KEY (user_id, item_id)
        REFERENCES gtd_items(user_id, item_id)
);

CREATE INDEX IF NOT EXISTS gtd_item_log_item_idx
    ON gtd_item_log (user_id, item_id, log_id);

CREATE TABLE IF NOT EXISTS gtd_settings (
    user_id bigint PRIMARY KEY REFERENCES web_users(id) ON DELETE CASCADE,
    contexts text[] NOT NULL DEFAULT '{home,errands,calls,computer}',
    review_day text
        CHECK (review_day IN ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')),
    review_progress text,
    last_review_done date,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gtd_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE gtd_items FORCE ROW LEVEL SECURITY;
CREATE POLICY gtd_items_owner ON gtd_items
    USING (user_id = nullif(current_setting('precis.gtd_user_id', true), '')::bigint)
    WITH CHECK (user_id = nullif(current_setting('precis.gtd_user_id', true), '')::bigint);

ALTER TABLE gtd_item_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE gtd_item_log FORCE ROW LEVEL SECURITY;
CREATE POLICY gtd_item_log_owner ON gtd_item_log
    USING (user_id = nullif(current_setting('precis.gtd_user_id', true), '')::bigint)
    WITH CHECK (user_id = nullif(current_setting('precis.gtd_user_id', true), '')::bigint);

ALTER TABLE gtd_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE gtd_settings FORCE ROW LEVEL SECURITY;
CREATE POLICY gtd_settings_owner ON gtd_settings
    USING (user_id = nullif(current_setting('precis.gtd_user_id', true), '')::bigint)
    WITH CHECK (user_id = nullif(current_setting('precis.gtd_user_id', true), '')::bigint);

COMMIT;
