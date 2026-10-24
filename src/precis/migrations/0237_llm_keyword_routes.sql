-- 0237_llm_keyword_routes.sql
--
-- docs/backlog/llm-keyword-route.md: content-keyword model routing. One
-- ordered rule list evaluated at the router seam
-- (utils/llm/keyword_route.py): the first enabled, in-scope rule whose
-- pattern hits the request text retargets the call.
--
--   position   — match order (ascending; the /llm/rules page reorders by
--                swap + renumber).
--   pattern    — a plain keyword (whole-word, case-insensitive in code) or
--                a regex when is_regex is set (length-capped, compiled
--                once per snapshot load).
--   is_regex   — treat pattern as a regex; false = whole-word literal.
--   target     — the llm.op.* override grammar:
--                {"tier": <str>} | {"model": <str>} | {"chain": [rung…]}.
--   sources    — restrict the rule to these req.source values;
--                NULL = all sources.
--   tiers      — restrict to these tier values; NULL = all tiers.
--   enabled    — disabled rows are kept but never match (the operator's
--                "delete").
--   note       — operator-facing why.
--
-- Seeded empty — shipped defaults live in keyword_route.CODE_RULES.
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS llm_keyword_routes (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    position   integer NOT NULL,
    pattern    text NOT NULL,
    is_regex   boolean NOT NULL DEFAULT false,
    target     jsonb NOT NULL,
    sources    text[] NULL,
    tiers      text[] NULL,
    enabled    boolean NOT NULL DEFAULT true,
    note       text NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMIT;
