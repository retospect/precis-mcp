-- 0204_llm_call_log_reason_class.sql
--
-- Promote the router's failure class to a real column.
--
-- Since the failure-classification slice shipped, the class (quota / budget /
-- rate / transport / content) and its retry horizon have lived only inside
-- `llm_call_log.features` (`features->>'reason_class'`, `features->>'retry_at'`
-- — stamped by `router._route_features`). That answers the 30-day by-class
-- query, but only as a jsonb extraction: no index, and every dashboard row
-- re-parses the document. The promoted column makes the class a first-class
-- ledger field — groupable, indexable, and readable without remembering the
-- jsonb spelling.
--
-- `reason_class` is written from `LlmResult.reason_class` at dispatch time.
-- The `features` keys are still written too: `retry_at` has no column and the
-- jsonb copy keeps the per-row payload self-contained for blob replays — the
-- column is the queryable projection, not the only store. NULL = a successful
-- call, an unclassified failure, a pre-0204 row, or a non-router writer.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS reason_class text;

COMMENT ON COLUMN llm_call_log.reason_class IS
    'Failure class stamped by the router (LlmResult.reason_class): quota | '
    'budget | rate | transport | content. Also mirrored under '
    'features->>''reason_class'' (with features->>''retry_at'' carrying the '
    'horizon). NULL = success / unclassified / pre-0204 row.';

-- The hot query is "failures by class over the trailing N days" — partial on
-- the classification so the index stays small and serves exactly it.
CREATE INDEX IF NOT EXISTS llm_call_log_reason_class_ts_idx
    ON llm_call_log (reason_class, ts DESC)
    WHERE reason_class IS NOT NULL;

COMMIT;
