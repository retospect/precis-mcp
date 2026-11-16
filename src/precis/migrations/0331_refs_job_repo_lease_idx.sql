-- 0331_refs_job_repo_lease_idx.sql
--
-- gr685472 (prod EXPLAIN ANALYZE, 2026-11-15): the per-repo lease scan in
-- ``workers/repo_lease._rows`` walks every job row — a Parallel Index
-- Scan Backward over all ~224k ``kind = 'job'`` refs, job_type and the
-- lease predicates applied as a filter, ~190 ms and ~268k shared buffer
-- hits per job type, ~370 ms per holder()/acquire() call across both
-- job types. Every parked second-repo land or release pays it each
-- 120 s poll, and the cost grows with the jobs table. ``plans/
-- release.live_deploy_job`` has the same newest-50 shape and walks the
-- same table.
--
-- Two partial indexes, both near-empty by construction, same pattern as
-- 0282's ``refs_orphan_handle_idx`` — the predicate text mirrors the
-- query's WHERE clause verbatim, because the planner only uses a
-- partial index when the query implies its predicate:
--
--   * ``refs_job_repo_lease_idx`` covers the rows that can ever decide
--     a lease — jobs still carrying ``meta.repo_lease`` or
--     ``meta.repo_lease_wanted`` — and orders them by ref_id so the
--     ``ORDER BY ref_id DESC LIMIT 50`` scan reads them newest-first
--     with no sort. ``repo_lease._CANDIDATE_SQL`` carries the matching
--     ``meta ? 'repo_lease'`` clause so the predicate stays implied; a
--     test pins the two together. The index gains a row only while a
--     job holds or wants a lease: at most a handful.
--   * ``refs_job_release_deploy_idx`` covers ``live_deploy_job``'s
--     ``meta->>'job_type' = 'release_deploy'`` filter — a few dozen
--     rows ever, so the newest-50 read stops at the index.
--
-- Plain CREATE INDEX: Migrator.apply_all() wraps each file in a
-- transaction (CONCURRENTLY cannot run there), and migrations apply in
-- the deploy's single-writer window. Both builds scan the ~568k-row
-- heap once but insert almost nothing; the SHARE lock holds for
-- seconds, per the 0255/0282 precedent. A 5s lock_timeout keeps the
-- build from queueing behind open writers.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- at release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS refs_job_repo_lease_idx
    ON refs (ref_id)
    WHERE kind = 'job'
      AND retired_at IS NULL
      AND (meta ? 'repo_lease' OR meta ? 'repo_lease_wanted');

CREATE INDEX IF NOT EXISTS refs_job_release_deploy_idx
    ON refs (ref_id)
    WHERE kind = 'job'
      AND retired_at IS NULL
      AND meta ->> 'job_type' = 'release_deploy';

COMMIT;

-- End of 0331_refs_job_repo_lease_idx.sql
