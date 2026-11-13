-- 0317_slice_run_features.sql
--
-- gr595544 (task-aware routing, spec me568172 / demand contract
-- me568185): one cheap features row per launched `run_slice` attempt —
-- the substrate a learned router can be fit on. Today the only
-- pre-analysis path is the model-based requirement classifier
-- (td568400 S0b, parked 2026-11-08); these columns are extracted in
-- microseconds with no model call: prompt length, handle count, the
-- two-hop link neighbourhood, plan dependency depth, constraint-clause
-- count, operation type, the novelty distance to the nearest past
-- prompt embedding, and the submitting agent / task kind / lane class
-- the pick already knew. Keyed `(job_ref_id, launch)`: a row is one
-- LAUNCH, not one job — `run_slice._relaunch_when_due()` resubmits the
-- same `kind='job'` ref after a parked outage, so a job-only key would
-- drop every launch after the first. `attempt` stays a plain feature:
-- the burned-attempt ordinal off the todo's `attempt:<n>` tags, which
-- an outage relaunch does not spend. ON DELETE CASCADE so a reaped
-- job's rows go away with it.
--
-- `prompt_embedding` / `novelty_cosine` are NULL until the row's text
-- meets a reachable embedder — the writer fills them in one bounded
-- call when `PRECIS_EMBEDDER_URL` is set; a row without a vector is a
-- novelty gap, not a missing run. `prompt_text` keeps the embedded
-- task text so a later backfill can vectorise rows written while the
-- service was down.
--
-- No HNSW on `prompt_embedding`: the novelty read is one
-- `min(<=>)` scan over a few hundred rows. Add the index when the row
-- count makes the seq scan hurt.
--
-- Forward-only (ADR 0005). Idempotent (`CREATE TABLE IF NOT EXISTS`).

BEGIN;

CREATE TABLE IF NOT EXISTS slice_run_features (
    job_ref_id bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    todo_ref_id bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    attempt integer NOT NULL,
    launch integer NOT NULL,
    task_kind text,
    node_kind text,
    lane text,
    lane_class text,
    model text,
    effort text,
    submitter text,
    prompt_chars integer NOT NULL,
    handle_count integer NOT NULL,
    fisheye_2hop_nodes integer,
    max_dep_depth integer,
    constraint_clauses integer NOT NULL,
    op_type text,
    novelty_cosine real,
    prompt_sha256 text CHECK (char_length(prompt_sha256) = 64),
    prompt_text text,
    prompt_embedder text,
    prompt_embedding public.vector(1024),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_ref_id, launch)
);

-- Cover the todo FK (the PK leads job_ref_id): a todo reap or the
-- per-todo outcome join must not seq-scan the table.
CREATE INDEX IF NOT EXISTS slice_run_features_todo_ref_idx
    ON slice_run_features (todo_ref_id);

COMMENT ON TABLE slice_run_features IS
    'gr595544: one cheap features row per run_slice launch — '
    'text/graph features plus the pick context, the fit substrate for '
    'task-aware routing (me568172). prompt_embedding/novelty_cosine are '
    'NULL until a reachable embedder fills them.';

COMMIT;

-- End of 0317_slice_run_features.sql
