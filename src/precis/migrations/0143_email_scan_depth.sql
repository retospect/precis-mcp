-- 0143_email_scan_depth.sql
--
-- Vocabulary-compaction Stage C (docs/backlog/vocab-compaction-stages.md):
-- `email_scan.tier` -> `depth`. `Tier` is an overloaded homonym across the
-- repo (LLM router capability band, quest fidelity ladder, this scan-depth
-- counter); this column was never a "capability tier" — it is how many
-- passes deep the injection-scan cascade has reached a message (0 =
-- mail_poll's inline regex, 1 = the model rung, 2 = the escalated model).
-- `depth` names that without colliding with the router's `Tier`.
--
-- Per the plan's Deploy protocol, this ships together with the code that
-- reads/writes the renamed column (`store/_email_ops.py`,
-- `workers/inject_scan.py`, `workers/mail_poll.py`) behind a fleet quiesce —
-- no old binary ever reads `tier` after this lands, so a plain rename (no
-- read-both shim) is safe.
--
-- Forward-only (ADR 0005). The DO-block guard makes a repeat run (or a
-- fresh baseline already carrying `depth`) a no-op — a bare RENAME is not
-- naturally idempotent.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'email_scan'
          AND column_name = 'tier'
    ) THEN
        ALTER TABLE email_scan RENAME COLUMN tier TO depth;
    END IF;
END $$;

-- The partial index (`email_scan_pending_idx`, on (account, tier) WHERE
-- tier < 1) needs no DDL here -- Postgres rewrites an index's stored
-- definition to track a renamed column automatically; \d already shows
-- `depth` in its predicate without a drop+recreate.

COMMENT ON TABLE email_scan IS
    'Per-message injection-scan verdict for the email kind (no body stored); '
    'keyed by (account,folder,uidvalidity,uid). depth 0 = mail_poll regex.';

COMMENT ON COLUMN email_scan.depth IS
    'How many scan passes deep this verdict reached: 0 = mail_poll''s inline '
    'regex, 1 = the model rung, 2 = the escalated model (workers/inject_scan.py). '
    'Renamed from `tier` (vocab-compaction Stage C) to stop colliding with the '
    'LLM router''s unrelated `Tier` capability band.';

COMMIT;

-- End of 0143_email_scan_depth.sql
