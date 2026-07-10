-- 0055_rename_structure_cursor_to_eye.sql
--
-- Structure-kind terminology (ADR 0043 §6.6/§6.8, ADR 0053 §10): the
-- persisted observer discriminator `struct_measures.kind = 'cursor'` is
-- renamed to 'eye'. "Eyes" is the sole name going forward — the MCP tool
-- and skill surface carry no legacy alias, and the surface is transient
-- (a fresh LLM never sees the old vocabulary), so this one-shot data
-- rewrite is the only place the old value can still linger: rows persisted
-- under the previous name are realigned to 'eye'.
--
-- `struct_measures.kind` is bare `text` (no CHECK constraint), so a plain
-- UPDATE suffices — nothing else in the schema references the value.
--
-- Forward-only (ADR 0005). Idempotent (a re-run matches zero rows).

BEGIN;

UPDATE struct_measures SET kind = 'eye' WHERE kind = 'cursor';

COMMIT;

-- End of 0055_rename_structure_cursor_to_eye.sql
