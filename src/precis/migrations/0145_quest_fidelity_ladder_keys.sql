-- 0145_quest_fidelity_ladder_keys.sql
--
-- Vocabulary-compaction Stage C (docs/backlog/vocab-compaction-stages.md):
-- rename the quest catalyst ladder's `meta` keys off the overloaded `tier`
-- word onto `fidelity` — the word `quest/compute.py` already uses for the
-- SAME concept in its `_TIER_FIDELITY` rank table. `Tier` itself (the
-- screening/neb/verify RUNG label — `structure.meta.tier`,
-- `dispatch_autocatpath(tier=...)`, `_TIER_SCREENING`/`_TIER_NEB`/
-- `_TIER_VERIFY`) is untouched: it's a different, still-unambiguous sense
-- (which rung), left for a later pass if ever renamed at all. Only the
-- three ladder-bookkeeping keys move:
--
--   quest meta.tier_ladder            -> meta.fidelity_ladder
--   quest meta.tier_promote_neb       -> meta.fidelity_promote_neb
--   quest meta.tier_promote_verify    -> meta.fidelity_promote_verify
--   structure meta.barrier_tier       -> meta.barrier_fidelity
--
-- Each UPDATE only touches rows still carrying the source key (idempotent —
-- a fresh DB or an already-migrated row is a no-op). Ships with the code
-- that reads/writes these keys (`quest/catalyst_seed.py`, `quest/compute.py`,
-- `quest/graduate.py`, `quest/frontier.py`, `quest/rulings.py`,
-- `quest/figures.py`) behind a fleet quiesce.
--
-- Forward-only (ADR 0005).

BEGIN;

UPDATE refs
   SET meta = (meta - 'tier_ladder') || jsonb_build_object('fidelity_ladder', meta->'tier_ladder')
 WHERE kind = 'quest'
   AND meta ? 'tier_ladder';

UPDATE refs
   SET meta = (meta - 'tier_promote_neb')
             || jsonb_build_object('fidelity_promote_neb', meta->'tier_promote_neb')
 WHERE kind = 'quest'
   AND meta ? 'tier_promote_neb';

UPDATE refs
   SET meta = (meta - 'tier_promote_verify')
             || jsonb_build_object('fidelity_promote_verify', meta->'tier_promote_verify')
 WHERE kind = 'quest'
   AND meta ? 'tier_promote_verify';

UPDATE refs
   SET meta = (meta - 'barrier_tier') || jsonb_build_object('barrier_fidelity', meta->'barrier_tier')
 WHERE kind = 'structure'
   AND meta ? 'barrier_tier';

COMMIT;

-- End of 0145_quest_fidelity_ladder_keys.sql
