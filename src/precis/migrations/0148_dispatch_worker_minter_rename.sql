-- 0148_dispatch_worker_minter_rename.sql
--
-- Vocabulary-compaction Stage D (docs/backlog/vocab-compaction-stages.md):
-- the worker registered under `workers/registry.py` as `dispatch` (mints
-- `kind='job'` children under executor-bearing todos) renames to `minter`
-- — the CLI `--only`/`--profile` selector, the registry row, and the skill
-- id (`precis-dispatch-help` -> `precis-minter-help`) change with it.
-- `src/precis/dispatch.py` (the Hub verb table), `runtime/dispatch.py`,
-- `router.dispatch`/`route()`, and `dispatch_autocatpath(tier=)` are OTHER,
-- unrelated senses of "dispatch" and are untouched.
--
-- Two persisted surfaces name this worker by its old name:
--
-- 1. `service_config.service` -- an operator may have a live per-host
--    prio/model_pref/concurrency row keyed 'dispatch' (`set_service_prio`).
--    Renaming it here keeps that row attached to the pass it actually
--    controls after the code ships (else it silently orphans: a stale
--    'dispatch' row no longer matches anything `_should_register` reads).
-- 2. `ref_events.source = 'dispatch'` -- the job-minted event this worker
--    appends on the parent todo (`workers/dispatch.py::_claim_and_dispatch`).
--    DECIDED (Reto 2026-08-30): rewrite history too, not just new writes --
--    full vocabulary consistency wins over literal provenance here (this
--    is an internal worker-identity marker, not source-of-record evidence).
--
-- The worker's *module* (`workers/dispatch.py`) and its `worker_logs`
-- attribution (`registry.py`'s `log_name="dispatch"`, matching the
-- `fetch`->`fetch_oa` divergence pattern) are deliberately NOT renamed by
-- this stage -- see the stage doc's "surface renames" scoping.
--
-- Ships with the code that reads/writes these strings (`cli/worker.py`,
-- `workers/registry.py`, `workers/dispatch.py`) behind a fleet quiesce, per
-- the plan's Deploy protocol -- no old binary reads/writes 'dispatch' as
-- this worker's identity after this lands.
--
-- Forward-only (ADR 0005). `WHERE old-value` guards make a repeat run (or a
-- fresh DB with no 'dispatch' rows at all) a no-op.

BEGIN;

UPDATE service_config
   SET service = 'minter'
 WHERE service = 'dispatch';

UPDATE ref_events
   SET source = 'minter'
 WHERE source = 'dispatch';

COMMIT;

-- End of 0148_dispatch_worker_minter_rename.sql
