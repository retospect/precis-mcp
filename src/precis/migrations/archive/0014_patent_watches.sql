-- ===========================================================================
-- 0014_patent_watches.sql — create the patent_watches table.
--
-- ``src/precis/handlers/_patent_watch_db.py`` is the DAO for this
-- table; it expects the columns enumerated below. No migration ever
-- created it — the table was committed to code but never to schema.
-- Surfaced as ~15 test failures in tests/test_patent_watch_db.py
-- (and dependent cluster failures in test_patent_watch.py +
-- test_patent_watch_cli.py) once the PG-gated suite started running.
--
-- Column contract (mirrors INSERT / SELECT shapes in the DAO):
--   name        UNIQUE — caller-facing handle, normalised to lower
--   cql         the search expression for EPO OPS / equivalent
--   interval_s  retry cadence (e.g. 7 days = 604_800)
--   last_run_at / last_seen_pn — runner bookkeeping
--   auto_get / max_per_pass    — runner policy knobs
--   created_at / created_by    — provenance
--
-- The ``last_run_at IS NULL OR last_run_at + interval_s <= now()``
-- predicate is the runner's claim shape — an index on
-- ``(last_run_at, interval_s)`` keeps that scan cheap as the watch
-- list grows.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS patent_watches (
    id            BIGSERIAL    PRIMARY KEY,
    name          TEXT         NOT NULL UNIQUE,
    cql           TEXT         NOT NULL,
    interval_s    INTEGER      NOT NULL CHECK (interval_s > 0),
    auto_get      BOOLEAN      NOT NULL DEFAULT FALSE,
    max_per_pass  INTEGER      CHECK (max_per_pass IS NULL OR max_per_pass > 0),
    last_run_at   TIMESTAMPTZ,
    last_seen_pn  TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by    TEXT         NOT NULL
);

-- Runner-side claim path: "what watches are due?". Cheap when the
-- watch list grows large.
CREATE INDEX IF NOT EXISTS patent_watches_due_idx
    ON patent_watches (last_run_at NULLS FIRST);

-- ===========================================================================
-- End of 0014_patent_watches.sql
-- ===========================================================================
