-- 0007_dreaming.sql
--
-- Foundation schema for the dreaming capability (see
-- docs/design/dreaming.md). Additive only; forward-only (ADR 0005).
-- Every statement is idempotent so a re-run after a partial apply is
-- safe.
--
-- Changes:
--
--   1. Salience columns on `chunks`. Two timestamps + a counter that
--      drive dream target selection (`score = last_seen - last_dreamt`,
--      argmax wins). These are METADATA-ONLY: no content, no
--      embedding/summary cascade, so mutating them does not breach the
--      "chunks body is append-only" invariant (which guards
--      `chunks.text`). `accesses` is heatmap/observability only and
--      never enters the ranking.
--
--   2. `bump_salience(ids)` — one set-based, in-DB function so the
--      search path can record an access for a whole result page in a
--      single round-trip. thresholds.md is relaxed to permit
--      metadata-only writes on the search path (content stays
--      immutable).
--
--   3. Relations `supersedes` / `superseded-by` (mutual inverses) for
--      guarded consolidation. Distinct from `retracts` (retraction =
--      "this was wrong"; supersession = "absorbed into a better
--      phrasing"). Mirror in the `Relation` Literal + `_INVERSE_RELATIONS`
--      in store/types.py.
--
--   4. `dream_log` + `dream_transcripts` tables. One `dream_log` row per
--      agentic run (outcome/cost/turns); the full tool-call trace lives
--      in the 1:1 `dream_transcripts` sibling, kept separate so
--      `dream_log` stays lean for analytics scans. Analysis-only;
--      nothing here surfaces in normal search.

BEGIN;

-- 1. Salience columns on chunks (metadata-only).
ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS last_seen   timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS last_dreamt timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS accesses    integer     NOT NULL DEFAULT 0;

-- Everything starts at its birth: neither hot nor suppressed. (The
-- column DEFAULT now() would otherwise stamp existing rows at apply
-- time, which is wrong for the date-rotation seed.)
UPDATE chunks SET last_seen = created_at, last_dreamt = created_at;

-- Selection-key index: argmax(last_seen - last_dreamt) over target
-- kinds. A plain expression index keeps the seed query off a seq scan
-- as the corpus grows.
CREATE INDEX IF NOT EXISTS chunks_dream_score_idx
    ON chunks ((last_seen - last_dreamt) DESC);

-- 2. One set-based, in-DB bump: a single round-trip for a result page.
CREATE OR REPLACE FUNCTION bump_salience(ids bigint[]) RETURNS void
LANGUAGE sql AS $$
    UPDATE chunks SET last_seen = now(), accesses = accesses + 1
    WHERE chunk_id = ANY(ids);
$$;

-- 3. Consolidation relations (mutual inverses).
INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('supersedes', FALSE, 'superseded-by',
     'Source memory absorbs/replaces target (consolidation; target soft-deleted)'),
    ('superseded-by', FALSE, 'supersedes',
     'Source memory was absorbed into / replaced by target')
ON CONFLICT (slug) DO NOTHING;

-- 4. Dream telemetry.
CREATE TABLE IF NOT EXISTS dream_log (
    attempt_id       bigserial PRIMARY KEY,
    created_at       timestamptz NOT NULL DEFAULT now(),
    outcome          text NOT NULL,        -- wrote | noop | error
    behaviors        text[],               -- consolidate|synthesize|toc|inspire|acquire
    seed_clusters    jsonb,                -- region(s) it started from (member ids)
    result_ref_ids   bigint[],             -- refs created/affected (empty on noop)
    turns            integer,              -- agent turns used
    tool_calls       integer,              -- total MCP calls
    model            text,
    cost_usd         double precision,
    summary          jsonb                 -- agent's closing note + counts
);

-- 1:1 sibling, kept separate so dream_log stays lean for analytics.
CREATE TABLE IF NOT EXISTS dream_transcripts (
    attempt_id       bigint PRIMARY KEY REFERENCES dream_log(attempt_id),
    transcript       jsonb NOT NULL        -- full tool-call trace (args + results)
);

CREATE INDEX IF NOT EXISTS dream_log_outcome_created_idx
    ON dream_log (outcome, created_at);
CREATE INDEX IF NOT EXISTS dream_log_behaviors_gin_idx
    ON dream_log USING gin (behaviors);

COMMIT;

-- End of 0007_dreaming.sql
