-- 0090_llm_tier_floor_relabel.sql
--
-- ADR 0066 Phase C — relabel `llm` catalog cards' `meta.tier_floor` from the
-- legacy location-coupled tier values to the four pure-capability tiers. The
-- call-site sweep (main 5f1d9cb3) already flipped callers to
-- FRONTIER/BIG/MEDIUM/SMALL; `seed_default_cards` / `_FRONTIER_CARDS` now emit
-- the new values too, so this brings the already-minted rows into line (and
-- the reseed won't clobber them back — both write the same new values).
--
-- Mapping: cloud-super→frontier, cloud-mid→big, cloud-small→medium,
-- local-small→small, local-big→big. Note BOTH cloud-mid and local-big collapse
-- onto `big` (a tier may back several candidate models; placement is the
-- chain's job now, not the tier name). Idempotent: each UPDATE only touches
-- rows still carrying the old value, so re-running is a no-op.

UPDATE refs SET meta = jsonb_set(meta, '{tier_floor}', '"frontier"')
  WHERE kind = 'llm' AND meta->>'tier_floor' = 'cloud-super';

UPDATE refs SET meta = jsonb_set(meta, '{tier_floor}', '"big"')
  WHERE kind = 'llm' AND meta->>'tier_floor' = 'cloud-mid';

UPDATE refs SET meta = jsonb_set(meta, '{tier_floor}', '"medium"')
  WHERE kind = 'llm' AND meta->>'tier_floor' = 'cloud-small';

UPDATE refs SET meta = jsonb_set(meta, '{tier_floor}', '"small"')
  WHERE kind = 'llm' AND meta->>'tier_floor' = 'local-small';

UPDATE refs SET meta = jsonb_set(meta, '{tier_floor}', '"big"')
  WHERE kind = 'llm' AND meta->>'tier_floor' = 'local-big';
