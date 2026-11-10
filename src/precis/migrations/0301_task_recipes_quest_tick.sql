-- 0301_task_recipes_quest_tick.sql
--
-- gr572099: the quest tick's commit-ladder rungs log their LLM calls
-- under ``source = 'quest_tick_commit'``
-- (``quest/tick.py``'s ``_COMMIT_SOURCE`` — the "COMMIT NOW" re-prompt
-- the anti-stall ladder fires when a quest has proposed nothing for N
-- ticks), and no task_recipes row covered it — the recipe-gap scan
-- filed it as an unknown task kind.
--
-- The ``quest_tick_`` siblings are not a ``:``-terminated family, so
-- the prefix-family mechanism (``axis:``, ``review:``) cannot cover
-- them; the two unregistered tick-lane sources are enumerated instead
-- so the fix does not just move the gripe to the next one
-- (``quest_tick_narrative_compress`` — the growth-ratchet's compress
-- re-prompt, ``_COMPRESS_SOURCE`` — fires from the same tick and would
-- refile the same gripe the first time it trips, exactly the
-- gr572091→093 axis whack-a-mole).
--
-- Same demand as the primary ``quest_tick`` row: a tick-lane call at
-- the tick's resolved tier producing the tick's own output (the commit
-- rung carries the identical prompt plus the commit directive and the
-- same structured-proposals contract; the compress re-prompt rewrites
-- the dossier under the same stakes — a bad compress drops rulings).
--
-- Forward-only (ADR 0005). Idempotent (ON CONFLICT DO NOTHING — a
-- sibling slice that landed the same row first makes this a no-op
-- rather than a failure). Regenerate the baseline snapshot at release
-- time (ADR 0031): `scripts/bump` / `precis db dump-schema`.
INSERT INTO task_recipes (source, task_kind, lane_class, consequence, classify) VALUES
    ('quest_tick_commit', 'plan.tick', 'none', 'medium', true),
    ('quest_tick_narrative_compress', 'plan.tick', 'none', 'medium', true)
ON CONFLICT (source) DO NOTHING;

-- End of 0301_task_recipes_quest_tick.sql
