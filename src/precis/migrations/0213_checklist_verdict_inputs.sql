-- 0213_checklist_verdict_inputs.sql
--
-- pcb-datasheet-review slice 4 (in-scope 6, "re-derivable verdicts"):
-- a verdict records WHO produced it and WHAT it consumed.
--
--   * `checklist_verdicts.origin` — 'human' | 'machine'. The split the
--     spec's forget semantics need: machine verdicts are disposable
--     (`op='forget'` retires them so the next pass recomputes); human
--     verdicts are never dropped — they go stale and stay shown. Every
--     existing row came from the interactive op='verdict' path, so
--     DEFAULT 'human' backfills them honestly.
--   * `checklist_verdicts.inputs` — {input-name: revision} the verdict
--     consumed (margin/policy revisions, later part-fact ids+revs, rule
--     and skill revisions; the anchor fingerprint keeps its own
--     column). Staleness is read-time: the kind's bridge resolves an
--     input name's CURRENT revision; a mismatch renders
--     "stale (input changed: <name>)". A name nothing can resolve is
--     never a staleness trigger — you cannot fault on what you cannot
--     re-derive.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE checklist_verdicts
    ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'human'
        CHECK (origin IN ('human', 'machine'));

COMMENT ON COLUMN checklist_verdicts.origin IS
    'Who produced the verdict: human (sticky — op=''forget'' keeps it; '
    'shown stale, never replaced) or machine (disposable — forget drops '
    'it for re-derivation). pcb-datasheet-review slice 4.';

ALTER TABLE checklist_verdicts
    ADD COLUMN IF NOT EXISTS inputs jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN checklist_verdicts.inputs IS
    '{input-name: revision} the verdict consumed — e.g. '
    '"margin:fixed-derating": 1, later part-fact ids+revs and rule/skill '
    'revisions. The kind bridge resolves a name''s current revision at '
    'read time; a mismatch marks the verdict stale (input changed). '
    'Unresolvable names never mark stale.';

COMMIT;

-- End of 0213_checklist_verdict_inputs.sql
