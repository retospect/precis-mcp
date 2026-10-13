-- 0206_pcb_measure_strength_invariant.sql
--
-- gr345859 (docs/backlog/pcb-missing-constraint-classes.md §F, ruled
-- 2026-10-12): a fourth measure strength, 'invariant'. A 'hard' measure
-- only steers the anneal (~40x cost); an 'invariant' one is never
-- weighted — it is the generator-declared fabric rule that gates put/
-- route through the always-valid gate, refusing a mutation whose
-- after-state violates it. The check constraint learns the new value;
-- rows already stored are untouched.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- CHECK widening has no ALTER form; drop + re-add, table is 4 rows.
-- squawk-ignore ban-drop-constraint
ALTER TABLE pcb_measures DROP CONSTRAINT IF EXISTS pcb_measures_strength_chk;
ALTER TABLE pcb_measures
    ADD CONSTRAINT pcb_measures_strength_chk
    CHECK (strength = ANY (ARRAY['hard'::text, 'soft'::text, 'gauge'::text, 'invariant'::text]))
    NOT VALID;

COMMIT;

-- Validate outside the DDL transaction (squawk constraint-missing-not-valid).
ALTER TABLE pcb_measures VALIDATE CONSTRAINT pcb_measures_strength_chk;

-- End of 0206_pcb_measure_strength_invariant.sql
