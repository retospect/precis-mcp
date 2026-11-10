-- 0299_task_recipes_axis_family.sql
--
-- gr572093: every ``data/axes/<id>.yaml`` classifier pass logs its LLM
-- calls under source ``axis:<id>`` (cli/_worker_passes.py wires one
-- DispatchClient per ``discover_axis_ids`` id), but ``task_recipes``
-- had no row for any of them — recipe_gap's 24 h scan filed "Missing
-- task recipe: axis:open-question".
--
-- One ``axis:`` prefix-family row covers every current axis and any
-- future one: ``recipes.lookup`` resolves ``axis:open-question`` and
-- each sibling through its longest-prefix rule, the same shape as
-- ``review:``/``markup:``/``diagram_propose:`` (0278). Demand matches
-- the other closed-vocabulary classifiers: ``chunk.classify`` kind,
-- ``low`` consequence (SMALL-tier single-label calls). The ``junk`` /
-- ``role3`` cascade axes never register an ``axis:<id>`` service — they
-- already log under ``classify``.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline
-- snapshot at release time (ADR 0031): `scripts/bump` /
-- `precis db dump-schema`.

BEGIN;

INSERT INTO task_recipes (source, task_kind, lane_class, consequence, classify) VALUES
    ('axis:', 'chunk.classify', 'none', 'low', false)
ON CONFLICT (source) DO NOTHING;

COMMIT;

-- End of 0299_task_recipes_axis_family.sql
