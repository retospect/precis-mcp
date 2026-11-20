-- 0336_slice_overlap_indexes.sql
--
-- gr732475: scope the dispatch overlap scan to slice jobs and stamped
-- todos. The job index also supports newest-land and build-attempt
-- lookups for a single parent.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS refs_slice_overlap_jobs_idx
    ON refs ((meta->>'job_type'), parent_id, ref_id DESC)
    WHERE kind = 'job' AND retired_at IS NULL
      AND meta->>'job_type' IN ('run_slice', 'land_slice');

CREATE INDEX IF NOT EXISTS refs_slice_pending_land_idx
    ON refs (ref_id)
    WHERE kind = 'todo' AND retired_at IS NULL
      AND jsonb_typeof(meta->'pending_land') = 'object';

COMMIT;
