-- 0279_revisions_writer.sql
--
-- Write attribution (docs/backlog/write-attribution.md): which model, process
-- and human stood behind each revision.
--
--   * revisions.writer: jsonb in the shape of meta.filed_by (actor, client,
--     session_id, source, agentlog_id, model, model_source, human, human_via).
--     NULL = nothing known; pre-existing rows stay NULL, never guessed.
--
--   * The stamp comes from the write context (precis.writer, sent by
--     precis.store.revision_context) through a BEFORE INSERT trigger, so
--     every path into revisions is covered without replacing 0214's
--     precis_log_revision / precis_trust_append_act, and a statement cannot
--     name its own writer. A later fold into the transaction's row (ON
--     CONFLICT) keeps the first stamp.
--
--   * Separate from precis.model on purpose: a model on a generated context
--     routes the write through the machine-edge gate.
--
-- Forward-only (ADR 0005). Metadata-only ADD COLUMN; the wait on revisions is
-- bounded so a busy table fails the apply rather than queueing writers.

BEGIN;

SET LOCAL lock_timeout = '10s';

CREATE OR REPLACE FUNCTION precis_revisions_writer()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.writer := precis_revision_setting('writer')::jsonb;
    RETURN NEW;
END
$$;

ALTER TABLE revisions ADD COLUMN IF NOT EXISTS writer jsonb;

COMMENT ON COLUMN revisions.writer IS
    'Who stood behind this revision, in the shape of meta.filed_by: actor, '
    'client, session_id, source, agentlog_id, model, model_source (agentlog | '
    'declared | caller | self), human, human_via (direct | mediated). Set from '
    'the write context only (0279). A labelled claim, not a trust rank; NULL = '
    'nothing known.';

DROP TRIGGER IF EXISTS revisions_writer ON revisions;
CREATE TRIGGER revisions_writer
    BEFORE INSERT ON revisions FOR EACH ROW
    EXECUTE FUNCTION precis_revisions_writer();

COMMIT;
