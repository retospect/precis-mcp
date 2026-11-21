-- 0344_anki_known_undo.sql
-- An owner can undo only their own retirement; mastery belongs to the shared concept.
CREATE TABLE IF NOT EXISTS anki_known_undo (
    concept_id bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    user_id bigint NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    token uuid NOT NULL,
    prior_mastery jsonb NOT NULL,
    card_retirements jsonb NOT NULL,
    PRIMARY KEY (user_id, concept_id)
);

CREATE INDEX IF NOT EXISTS anki_known_undo_concept_idx ON anki_known_undo (concept_id);
