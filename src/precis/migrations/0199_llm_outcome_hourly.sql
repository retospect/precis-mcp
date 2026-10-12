-- 0199_llm_outcome_hourly.sql
--
-- Hourly tally of paid LLM admission outcomes per work class (gr347551):
-- served / degraded / paused × interactive / deliverable / background.
-- /budget reads the last 24 buckets. A paused call is refused at the
-- budget gate before dispatch, so it never reaches llm_call_log; this
-- table is where it is counted (precis.budget.outcomes).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS llm_outcome_hourly (
    hour        timestamptz NOT NULL,
    work_class  text        NOT NULL,
    outcome     text        NOT NULL,
    n           bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (hour, work_class, outcome)
);

COMMENT ON TABLE llm_outcome_hourly IS
    'Hourly count of paid LLM gate outcomes (served/degraded/paused) per '
    'work class (interactive/deliverable/background); see '
    'precis.budget.outcomes.';

COMMIT;

-- End of 0199_llm_outcome_hourly.sql
