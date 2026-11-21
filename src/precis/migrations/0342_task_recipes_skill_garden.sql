-- 0342_task_recipes_skill_garden.sql
--
-- td724976: the monthly skill gardener's verdict call logs its LLM
-- usage under ``source = 'skill_garden:propose'``
-- (``workers/job_types/skill_garden.py``'s ``OP_SOURCE``) and no
-- task_recipes row covered it — the recipe-gap scan would file it as
-- an unknown task kind on the first monthly run.
--
-- Same demand as ``graph_garden:propose`` (0278): a low-consequence
-- proposer whose output is a ledger row a reviewer gates.
--
-- Forward-only (ADR 0005). Idempotent (ON CONFLICT DO NOTHING).
INSERT INTO task_recipes (source, task_kind, lane_class, consequence, classify) VALUES
    ('skill_garden:propose', 'memory.garden.propose', 'none', 'low', false)
ON CONFLICT (source) DO NOTHING;

-- End of 0342_task_recipes_skill_garden.sql
