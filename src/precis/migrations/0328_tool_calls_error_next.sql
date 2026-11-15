-- 0328_tool_calls_error_next.sql
--
-- Whether an error ack carried a concrete corrected call — the
-- follow-through meter's missing half (gr613469, td605104 baseline:
-- 129/130 sampled acks named only the generic
-- get(kind='skill', ...) doc pointer, and agents followed the very
-- next hint 10% of the time). ``error_head`` keeps only the ack's
-- first line, so ``next:`` uptake was not measurable from the ledger
-- at all. The dispatch chokepoint now parses the rendered error
-- body's ``  next:`` lines (``tool_ledger.next_call_summary``) and
-- records the first concrete call — a ``verb(`` line that is not the
-- ``get(kind='skill', …)`` doc pointer — so the memory-ablation
-- no-hints arm (td605103) has a prod counterpart.
--
-- Columns (all NULL on ok rows; verb/kind NULL when no concrete next):
--   * error_next_concrete: the ack named a runnable corrected call.
--   * error_next_verb / error_next_kind: the suggested call's verb and
--     kind — the join keys for "did the agent take the hint".
--
-- Forward-only (ADR 0005). No backfill — pre-0325 rows keep NULL; their
-- next: content was never recorded.

BEGIN;

ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS error_next_concrete boolean,
    ADD COLUMN IF NOT EXISTS error_next_verb text,
    ADD COLUMN IF NOT EXISTS error_next_kind text;

COMMENT ON COLUMN tool_calls.error_next_concrete IS
    'The rendered error ack named a concrete corrected call — a next: line that IS a call (get/search/put/edit/delete/tag/link() and not the get(kind=skill) doc pointer). NULL on ok rows.';
COMMENT ON COLUMN tool_calls.error_next_verb IS
    'Verb of the first concrete next: call on an error ack; NULL when the ack had no concrete next.';
COMMENT ON COLUMN tool_calls.error_next_kind IS
    'kind= named by the first concrete next: call on an error ack; NULL when the ack had no concrete next or the call named none.';

COMMIT;

-- End of 0328_tool_calls_error_next.sql
