-- 0322_model_footprint.sql
--
-- gr595542 (cluster-scheduling.md Pillar 3b, "Admission on the pull"): the
-- per-model VRAM footprint table the GPU admission checks against. One row
-- per model: weights + KV cache for the context length, "learnable from a
-- single run" — unlike the (job_type, meta.size) RSS rollup, one measured
-- run pins a model's VRAM, so the table is keyed on the model alone and
-- carries where the number came from.
--
-- The pull-side gate (``precis.workers.executors._common`` via
-- ``precis.workers.footprint_admission``) reads it at claim time: a GPU job
-- declaring ``params.model`` reserves ``vram_mb`` units (footprint + margin)
-- on the reservation host, all-or-nothing with its ``gpu`` token, but only
-- when the model row exists AND the host advertises a ``vram_mb`` capacity
-- row — either side missing leaves today's token-only admission unchanged
-- (the table starts empty; the feature is dark until rows are learned).
-- Per-run VRAM measurement (the learner that fills this table) is gr595537+.
--
-- Also here: the partial expression index the claim path's reserved-host
-- scans use (``_hold_rows`` / ``_pending_mb`` in footprint_admission.py:
-- ``WHERE kind = 'job' AND meta->'reserved'->>'host' = $1``). Every
-- admitted claim runs them under the resource_slots row lock, and without
-- an index each is a scan of all job refs. The predicate is just
-- ``kind = 'job'`` because the planner can't derive ``meta ? 'reserved'``
-- from the host equality, so a tighter predicate would never match; rows
-- without a reservation index as NULL and cost little.
--
-- Non-concurrent like 0313: migrations run in the deploy's single-writer
-- window; the 5s lock_timeout keeps the build from wedging behind writes.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS model_footprint (
    model            text PRIMARY KEY,
    vram_mb          integer NOT NULL CHECK (vram_mb > 0),
    context_tokens   integer CHECK (context_tokens IS NULL OR context_tokens > 0),
    learned_at       timestamptz NOT NULL DEFAULT now(),
    source           text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS refs_job_reserved_host_idx
    ON refs ((meta->'reserved'->>'host'))
    WHERE kind = 'job';

COMMIT;

-- End of 0322_model_footprint.sql
