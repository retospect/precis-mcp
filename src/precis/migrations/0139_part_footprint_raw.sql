-- 0139_part_footprint_raw.sql
--
-- docs/backlog/pcb-guided-place-route.md Slice 2 — cache the untouched
-- EasyEDA component JSON alongside the parsed footprint. `easyeda.py`
-- parses only the pad subset we need today; keeping the raw doc means a
-- future parser improvement (better courtyard recovery, 3D model ref, ...)
-- can reparse from the cache instead of re-fetching a third-party host.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE part_footprints ADD COLUMN IF NOT EXISTS raw jsonb;

COMMENT ON COLUMN part_footprints.raw IS
    'Untouched EasyEDA component JSON (GET easyeda.com/api/products/<C>/'
    'components) kept for reparse without re-fetching a third-party host.';

COMMIT;

-- End of 0139_part_footprint_raw.sql
