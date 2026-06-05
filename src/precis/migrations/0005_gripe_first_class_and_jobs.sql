-- 0005_gripe_first_class_and_jobs.sql
--
-- Open `gripe` up to a full first-class kind and add `job` as the
-- substrate for offline LLM-driven work (the first job_type is
-- `fix_gripe`, which drives a sandboxed `claude -p` to prepare a
-- candidate fix branch).
--
-- Schema additions are all data-only — every column required by
-- gripe and job already exists on the shared `refs` + `chunks`
-- tables. This migration only seeds the kind / chunk_kind
-- registries.
--
-- Changes:
--
--   1. Register `kind='job'` in kinds. Numeric-id, note-like —
--      same shape as gripe / todo / memory. Title carries the
--      human-readable goal; meta JSONB carries job_type, executor,
--      params, lease, result, etc. (See `precis-job-help`.)
--
--   2. Register `chunk_kind='gripe_comment'`. Append-only timeline
--      attached to a gripe ref; picked up by the embed +
--      chunk_keywords workers automatically so comments become
--      searchable through the normal chunk surface. is_card=FALSE
--      (per existing `gripe_body` precedent).
--
--   3. Register `chunk_kind='job_event'`. Worker telemetry from a
--      running job (subprocess stdout snippets, commit_made markers,
--      lease renewals, etc.). is_card=FALSE. Excluded from the
--      default search path by the application — these are stored
--      for forensics, not for retrieval.
--
--   4. Register `chunk_kind='job_summary'`. The human-readable
--      final account of a job ("Fix attempt pushed to origin as
--      branch gripe_42 @ <sha>. Diff +47/-12. Took 84s."). is_card
--      =FALSE. Searchable.
--
-- The existing `gripe_body` chunk_kind seed in 0001_initial.sql
-- stays untouched — it's been the body-chunk slug since gripe was
-- introduced, even though the v0 write-only handler never actually
-- materialised any chunks. The first-class handler in this PR
-- begins to write `gripe_body` rows on every put-create.
--
-- Forward-only (ADR 0005). Every statement is idempotent under
-- `ON CONFLICT DO NOTHING` so a re-run after a partial apply is
-- safe.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('job', TRUE, 'Job',
     'Offline run of a task — fix this gripe, run a simulation, '
     'benchmark a commit. Addressable by numeric id; status via '
     'STATUS: tags; comment timeline via job_event + job_summary '
     'chunks.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('gripe_comment', FALSE, 'Gripe comment / append-only timeline entry'),
    ('job_event',     FALSE, 'Job worker telemetry (forensics, not search)'),
    ('job_summary',   FALSE, 'Job completion summary (human-readable, searchable)')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0005_gripe_first_class_and_jobs.sql
