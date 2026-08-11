-- 0120_run_log_chunk_kind.sql
--
-- qu164903 catpath stall (2026-08-11): every `autocatpath_seed` job failed and
-- the quest's Pareto frontier never gained a converged candidate. Root cause:
-- `precis_pathway/seed_job.py` writes a `run_log` chunk on the seed's success
-- path (`ctx.append_chunk("run_log", ...)`, the tail of the compute child's
-- captured output), but `run_log` was never registered in `chunk_kinds`.
-- `chunks.chunk_kind` carries an FK to `chunk_kinds(slug)` (0001_initial.sql),
-- so the write raised `ForeignKeyViolation: Key (chunk_kind)=(run_log) is not
-- present in table "chunk_kinds"` and failed the job AFTER the compute had
-- already run. The commit that added the write (a0ff7270 "persist per-seed
-- run_log chunks") shipped no accompanying migration. It slipped CI because the
-- write is gated on `if tail:` (non-empty child output) and only fires on the
-- real detached ssh_node compute path, which unit tests don't exercise.
--
-- Same shape as the existing `job_event` / `job_summary` forensics chunk_kinds
-- (is_card FALSE, telemetry not a card) — no other wiring needed: `run_log` is
-- only ever appended by the seed job and read back by the explorer/compute
-- provenance (`precis/quest/compute.py`, `precis_web/routes/refs.py`); it is not
-- prose, so it joins no PROSE_CHUNK_KINDS-style allow-list.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('run_log', FALSE,
     'Per-seed autocatpath run-log chunk — the tail of the compute child''s '
     'captured stdout/stderr for one (model, seed) run. Forensics/provenance, '
     'not a search card (mirrors job_event / job_summary).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0120_run_log_chunk_kind.sql
