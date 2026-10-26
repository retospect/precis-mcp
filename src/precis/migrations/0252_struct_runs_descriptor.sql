-- 0252_struct_runs_descriptor.sql
--
-- Catalyst-library tie-in, slice A1 (docs/backlog/
-- catalyst-library-pd-slab-tie-in.md): an external run's `method` fingerprint
-- is the comparability key — two runs may only be subtracted for a ΔE when it
-- matches. Until now the catalysis-hub adapter also packed record-level
-- *descriptors* (facet, surface_composition, reactants, products) into the
-- same column, so two records from one paper and functional never counted as
-- the same method. The descriptors move to their own nullable payload here;
-- `method` keeps only the method fields (functional, code, cutoff_eV, kmesh,
-- spin, dataset_doi).
--
-- No backfill: pre-A1 rows keep descriptors inside `method` until the next
-- idempotent re-import rewrites both columns; readers fall back to `method`
-- for rows that never populated `descriptor`.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

ALTER TABLE struct_runs
    ADD COLUMN IF NOT EXISTS descriptor jsonb;

COMMENT ON COLUMN struct_runs.descriptor IS
    'Record-level descriptors of an imported run (facet, surface_composition, '
    'reactants, products) — what the record describes, not how it was '
    'computed (that is `method`, 0084). NULL for computed rows and for '
    'external rows imported before 0252 (readers fall back to `method`).';

COMMIT;

-- End of 0252_struct_runs_descriptor.sql
