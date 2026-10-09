-- 0192_checklist_pcb_tapeout.sql
--
-- checklist kind, slice 2 (docs/backlog/checklist-kind.md "pcb tapeout
-- instance"): the two columns the first real instance needs on top of
-- 0158's five tables.
--
--   * `checklist_verdicts.anchors` — the sub-object names (pcb: refdes /
--     net names) a verdict's fingerprint was narrowed to. Empty = the
--     verdict covered the whole target. Stored next to `fingerprint` so a
--     read can recompute the CURRENT fingerprint over the same scope and
--     compare; the fingerprint alone could not say what it hashed.
--     Name-anchored, never row-id (`pcb_apply` rebuilds rows on every
--     put — the same constraint that shaped se_notes). text[] rather than
--     jsonb: a flat list of names, nothing open-ended to hide.
--   * `checklists.default_for` — the ref kinds every target of which is
--     implicitly assigned this checklist ("every pcb gets pcb-tapeout").
--     Read next to `checklist_assignments`: a live explicit row OR the
--     target's kind in `default_for` counts as assigned. Owned by the
--     shipped YAML (`default_for: [pcb]`) through jobs/checklist_sync.py;
--     empty for local checklists unless put() sets it.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE checklist_verdicts
    ADD COLUMN IF NOT EXISTS anchors text[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN checklist_verdicts.anchors IS
    'Target sub-object names (pcb: refdes/net names) the fingerprint was '
    'narrowed to; empty = whole target. Lets a read recompute the current '
    'fingerprint over the same scope.';

ALTER TABLE checklists
    ADD COLUMN IF NOT EXISTS default_for text[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN checklists.default_for IS
    'refs.kind values whose every target is implicitly assigned this '
    'checklist (kind-default assignment). Set from the shipped YAML''s '
    'default_for by checklist_sync.';

COMMIT;

-- End of 0192_checklist_pcb_tapeout.sql
