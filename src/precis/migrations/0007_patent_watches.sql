-- 0007_patent_watches.sql — saved CQL watches for the ``patent`` kind.
--
-- Phase 2 of the patent kind. The runner (precis.jobs.patent_watch)
-- reads this table once an hour, picks rows that are due, runs their
-- CQL against EPO OPS, diffs the result publication numbers against
-- ``last_seen_pn``, and either opens a quest or auto-ingests the
-- delta. See @docs/patent-kind-spec.md § "Saved-watch table".
--
-- ``last_seen_pn`` grows monotonically while a watch is active; the
-- weekly vacuum cron picks up the array bloat. ``last_run_at NULL``
-- means "never run" — the due-index NULLS FIRST clause runs those
-- on the next pass.

CREATE TABLE patent_watches (
    id              SERIAL PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,                  -- slug; CLI's --name
    cql             TEXT NOT NULL,                         -- strict CQL only (validated at create time)
    interval_s      INTEGER NOT NULL DEFAULT 604800,       -- default 7 days
    last_run_at     TIMESTAMPTZ,                           -- NULL = never run
    last_seen_pn    TEXT[] NOT NULL DEFAULT '{}',          -- DOCDB ids seen previously
    auto_get        BOOLEAN NOT NULL DEFAULT FALSE,        -- ingest hits vs queue quests
    max_per_pass    INTEGER,                               -- nullable budget cap; NULL = unlimited
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT,                                  -- 'agent', 'system', or actor slug

    -- Sanity: interval has to be positive; ``max_per_pass`` if set
    -- must be positive too. A zero-budget watch would silently
    -- consume OPS quota and persist nothing, which is the worst of
    -- both worlds.
    CONSTRAINT patent_watches_interval_positive
        CHECK (interval_s > 0),
    CONSTRAINT patent_watches_max_per_pass_positive
        CHECK (max_per_pass IS NULL OR max_per_pass > 0)
);

-- Due-watch lookup index. The runner asks: "of every watch, which
-- have never run, or last ran more than ``interval_s`` ago?".
-- NULLS FIRST puts freshly-created watches at the head of the queue
-- on every pass without needing a second sort key.
CREATE INDEX patent_watches_due_idx
    ON patent_watches (last_run_at NULLS FIRST, interval_s);
