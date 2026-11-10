-- 0298_task_recipes_axis.sql
--
-- gr572091: the ``axis:domain`` classifier pass logs its LLM calls under
-- ``source = 'axis:domain'`` (``cli/_worker_passes.py`` builds one
-- DispatchClient per ``data/axes/<id>.yaml`` axis, named ``axis:<id>``),
-- and no task_recipes row covered it — the recipe-gap scan filed it as
-- an unknown task kind. An exact ``axis:domain`` row would only move the
-- gripe to the next enabled axis (material, move, …): one ``axis:``
-- prefix-family row covers the whole family — ``recipes.lookup``
-- resolves the longest ``:``-terminated prefix, the mechanism the seeded
-- ``review:`` row already uses — while an axis that ever needs its own
-- demand still wins through the exact-match step ahead of the family.
--
-- Same shape as its classify siblings (``classify``,
-- ``classify_topics``): single-label classification, low consequence,
-- no lane.
--
-- Forward-only (ADR 0005). Idempotent.
INSERT INTO task_recipes (source, task_kind, lane_class, consequence, classify) VALUES
    ('axis:', 'chunk.classify', 'none', 'low', false)
ON CONFLICT (source) DO NOTHING;
