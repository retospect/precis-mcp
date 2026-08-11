-- 0122_llm_call_log_token_counts.sql
--
-- Per-call token accounting. `LlmResult` already carries `input_tokens` /
-- `output_tokens` / `cache_read_tokens` / `cache_creation_tokens` (the
-- `claude_agent` stream-json `result` event's telemetry, normalized by
-- `result_from_agent`; the OSS `tools=` loop's `result_from_openai` reports
-- the same four from the completion's `usage` block) but `llm_call_log` never
-- persisted them — `_record_dispatch` (router.py) dropped them on the floor.
-- That leaves the log's own char-count volume signal (`request_chars` /
-- `response_chars`) as the only per-call size proxy, when the provider
-- already handed back the real token count.
--
-- All four nullable, no default, no backfill: `result_from_claude_p` (the
-- one-shot `claude -p` judge lane) still reports no token telemetry and
-- leaves them `None` — an honest gap, not a zero. A pre-existing row has no
-- token counts either, for the same reason `placement` (migration 0112)
-- wasn't backfilled: there is nothing to derive them from after the fact.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS input_tokens integer;
ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS output_tokens integer;
ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS cache_read_tokens integer;
ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS cache_creation_tokens integer;

COMMENT ON COLUMN llm_call_log.input_tokens IS
    'Prompt tokens reported by the provider (LlmResult.input_tokens). NULL '
    'when the transport reports none (e.g. claude_p) or predates this column.';
COMMENT ON COLUMN llm_call_log.output_tokens IS
    'Completion tokens reported by the provider (LlmResult.output_tokens).';
COMMENT ON COLUMN llm_call_log.cache_read_tokens IS
    'Prompt-cache-read tokens (LlmResult.cache_read_tokens) — billed at a '
    'discount, so kept separate from input_tokens rather than folded in.';
COMMENT ON COLUMN llm_call_log.cache_creation_tokens IS
    'Prompt-cache-write tokens (LlmResult.cache_creation_tokens).';

COMMIT;

-- End of 0122_llm_call_log_token_counts.sql
