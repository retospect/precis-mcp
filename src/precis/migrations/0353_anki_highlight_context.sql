-- 0353_anki_highlight_context.sql
-- Pending highlight text belongs to the requesting login, outside graph refs.
CREATE TABLE IF NOT EXISTS anki_highlight_context (
    concept_id bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    owner_login text NOT NULL REFERENCES web_users(login) ON UPDATE CASCADE ON DELETE CASCADE,
    submission text NOT NULL,
    context text NOT NULL,
    queued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (concept_id, owner_login, submission)
);

CREATE INDEX IF NOT EXISTS anki_highlight_context_owner_idx
    ON anki_highlight_context (owner_login);
