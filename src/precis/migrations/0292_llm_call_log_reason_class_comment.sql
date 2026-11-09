-- 0292_llm_call_log_reason_class_comment.sql
--
-- Re-document llm_call_log.reason_class: the stamped vocabulary grew past
-- the five classes migration 0204's comment names. The router now also
-- stamps ``unparseable`` (a completed call whose reply carried no parseable
-- JSON block — gr570522: ~18% of claude-sonnet-5 claude_p calls billed and
-- discarded on it) and the subscription-family refusals
-- (``subscription_cap`` / ``subscription_bulk_refused`` /
-- ``subscription_op_refused`` / ``subscription_worker_refused``,
-- gr543825/gr543829). The comment is what scripts/gen-schema renders into
-- docs/reference/schema.md, so the vocabulary a reader greps for lives
-- here — a class that exists in the data but not the comment reads as a
-- bug in the classifier, as gr570522's intake did.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

COMMENT ON COLUMN llm_call_log.reason_class IS
    'Failure class stamped by the router (LlmResult.reason_class): quota | '
    'budget | rate | transport | content | unparseable | subscription_cap | '
    'subscription_bulk_refused | subscription_op_refused | '
    'subscription_worker_refused. Also mirrored under '
    'features->>''reason_class'' (with features->>''retry_at'' carrying the '
    'horizon). NULL = success / unclassified / pre-0204 row.';

COMMIT;
