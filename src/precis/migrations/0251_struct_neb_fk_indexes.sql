-- 0251_struct_neb_fk_indexes.sql
--
-- Covering indexes for the three struct_neb_runs foreign keys 0250 left
-- bare (gr512126, NEB structure builder slice 3):
--
--   * is_run_id / fs_run_id -> struct_runs (id) ON DELETE SET NULL
--   * job_ref_id            -> refs (ref_id)    ON DELETE SET NULL
--
-- Without them a DELETE on a parent struct_runs / refs row seq-scans
-- struct_neb_runs to null the child pointer (tests/test_schema_design.py::
-- test_fk_columns_have_covering_index). Partial `WHERE col IS NOT NULL`:
-- all three columns are NULL on most rows, and that predicate is the shape
-- the FK-cascade lookup (`col = $1`) can use. Pure additions: no column,
-- constraint or existing index of 0250 changes.
--
-- struct_neb_runs is new in 0250, so a plain (non-CONCURRENT) build under
-- the transaction is cheap. Forward-only (ADR 0005). Idempotent.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS struct_neb_runs_is_run_idx
    ON struct_neb_runs (is_run_id) WHERE is_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS struct_neb_runs_fs_run_idx
    ON struct_neb_runs (fs_run_id) WHERE fs_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS struct_neb_runs_job_idx
    ON struct_neb_runs (job_ref_id) WHERE job_ref_id IS NOT NULL;

COMMIT;

-- End of 0251_struct_neb_fk_indexes.sql
