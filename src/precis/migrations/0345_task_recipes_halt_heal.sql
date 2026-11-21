-- 0345_task_recipes_halt_heal.sql
--
-- The halt supervisor's frontier session (workers/job_types/halt_heal.py)
-- logs its LLM usage under ``source = 'halt_heal'`` and no task_recipes row
-- covered it — the recipe-gap scan would file it as an unknown task kind on
-- the first halt.
--
-- Same demand shape as ``doctor_tick`` (an agent tick that reads the fleet's
-- published surfaces): ``plan.tick``. ``consequence`` is ``high``: a verdict
-- here empties a halt flag or pages a human. Neither field steers the
-- request: the router stamps them on the call log, and the shadow decider
-- keeps the request's own FRONTIER tier floor; ``consequence`` only ever
-- relaxes a marginal offering when it reads ``low``, so ``high`` cannot
-- lower or reroute the session. ``classify`` stays false (nothing reads it
-- for an agent tick).
--
-- Forward-only (ADR 0005). Idempotent (ON CONFLICT DO NOTHING).
INSERT INTO task_recipes (source, task_kind, lane_class, consequence, classify) VALUES
    ('halt_heal', 'plan.tick', 'none', 'high', false)
ON CONFLICT (source) DO NOTHING;

-- End of 0345_task_recipes_halt_heal.sql
