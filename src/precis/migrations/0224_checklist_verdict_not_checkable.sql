-- 0224_checklist_verdict_not_checkable.sql
--
-- pcb-datasheet-review, Reto's ruling 2026-10-16 (1a): "not checkable"
-- becomes a real `checklist_verdicts.verdict` value — "so we can see".
-- Until now the pcb_review pass wrote a keyed `question` note on the
-- item's thread when the evidence was missing, and the item stayed
-- "not checked" in every count. A 'not checkable' verdict is a current,
-- recorded answer (not re-asked until it goes stale — its `inputs`
-- carry the part-fact revisions, so extracting the missing facts
-- re-opens it) that is never a pass: tape-out counts it as open.
--
-- Forward-only (ADR 0005). Idempotent: the column CHECK is dropped by
-- its Postgres-default name and re-added under that name.

BEGIN;

SET LOCAL lock_timeout = '5s';

-- CHECK widening has no ALTER form; drop + re-add (superset, NOT VALID).
-- squawk-ignore ban-drop-constraint
ALTER TABLE checklist_verdicts DROP CONSTRAINT IF EXISTS checklist_verdicts_verdict_check;

ALTER TABLE checklist_verdicts
    ADD CONSTRAINT checklist_verdicts_verdict_check
        CHECK (verdict IN ('pass', 'fail', 'n/a', 'waived', 'not checkable'))
        NOT VALID;

COMMENT ON COLUMN checklist_verdicts.verdict IS
    'pass | fail | n/a | waived | not checkable. ''not checkable'' (0224) '
    'records that the evidence the item needs is missing (e.g. a part '
    'with no datasheet facts): settled until stale, never a pass, open '
    'for tape-out.';

COMMIT;

-- Validate outside the rewrite's transaction (squawk
-- constraint-missing-not-valid); the new set is a superset of the old one,
-- so this scan cannot fail.
ALTER TABLE checklist_verdicts
    VALIDATE CONSTRAINT checklist_verdicts_verdict_check;

-- End of 0224_checklist_verdict_not_checkable.sql
