-- 0212_measurand_higher_is_better.sql
--
-- `higher_is_better` for the two measurand taxa the survey-extraction
-- slice found without one (docs/backlog/measures-substrate.md; Reto
-- 2026-10-14). best_measure's rank reads it; NULL reads as "no
-- direction", so these two stayed unordered.
--
--   * total-current-density — an activity measure: higher is better.
--   * adsorption-energy — stored in the E_ads convention (a negative
--     value is a bound state; the live rows are -0.8 .. -1.9 eV in J).
--     For the reactant-adsorption readings this corpus collects, the
--     better direction is stronger binding = the LOWER (more negative)
--     number: higher_is_better = false.
--
-- Both UPDATEs are keyed on (slug, canonical_unit), not ref_id — slugs
-- are the portable identity across databases — and only fill a NULL
-- key: an explicitly set direction (a later human ruling) is never
-- overwritten, so the migration is idempotent as well as forward-only
-- (ADR 0005). No ref_id pins and no other measurand is touched: the
-- remaining NULL taxa (reaction-step free energy, the condition
-- measurands) have genuinely ambiguous directions and stay NULL.

BEGIN;

UPDATE refs
   SET meta = jsonb_set(meta, '{higher_is_better}', 'true'::jsonb)
 WHERE kind = 'taxon' AND retired_at IS NULL
   AND meta->>'slug' = 'total-current-density'
   AND meta->>'canonical_unit' = 'A/m^2'
   AND meta -> 'higher_is_better' IS NULL;

UPDATE refs
   SET meta = jsonb_set(meta, '{higher_is_better}', 'false'::jsonb)
 WHERE kind = 'taxon' AND retired_at IS NULL
   AND meta->>'slug' = 'adsorption-energy'
   AND meta->>'canonical_unit' = 'J'
   AND meta -> 'higher_is_better' IS NULL;

COMMIT;
