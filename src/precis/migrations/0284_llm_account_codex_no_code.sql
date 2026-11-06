-- 0284_llm_account_codex_no_code.sql
--
-- gr573563 / td573580: codex cannot reach the test gate's docker, so a
-- lane that cannot run the gate is never offered task_class 'code'.
-- The lane fact lives in the account row's task_classes — lanes.py
-- derives the table from llm_account (td568401 S1c), Lane.fits and the
-- retry ladder honour it, and so does the capacity count in
-- workers/gripe_dispatch.py. Review and validate keep codex: their one
-- verdict rides the session's final message and needs neither the MCP
-- nor the gate.
--
-- The guarded UPDATE leaves an owner's edit alone; the owner flips
-- 'code' back through the account door once codex can reach Docker.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

UPDATE llm_account SET task_classes = ARRAY['review','validate']
WHERE name = 'codex' AND task_classes = ARRAY['code','review','validate'];

COMMIT;

-- End of 0284_llm_account_codex_no_code.sql
