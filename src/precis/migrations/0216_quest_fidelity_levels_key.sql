-- 0216_quest_fidelity_levels_key.sql
--
-- Vocabulary alignment (docs/backlog/vocab-align-to-literature.md):
-- "fidelity ladder" -> "fidelity levels", the multi-fidelity literature's
-- term (Peherstorfer, Willcox & Gunzburger). Renames the quest opt-in key
-- that 0145 introduced:
--
--   quest meta.fidelity_ladder -> meta.fidelity_levels
--
-- `quest/catalyst_seed.py` is the only writer, so this covers every row and
-- the code reads the new key with no fallback. Idempotent: only rows still
-- carrying the old key are touched.
--
-- Forward-only (ADR 0005).

BEGIN;

UPDATE refs
   SET meta = (meta - 'fidelity_ladder')
             || jsonb_build_object('fidelity_levels', meta->'fidelity_ladder')
 WHERE kind = 'quest'
   AND meta ? 'fidelity_ladder';

COMMIT;

-- End of 0216_quest_fidelity_levels_key.sql
