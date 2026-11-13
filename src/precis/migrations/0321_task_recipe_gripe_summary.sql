-- 0321_task_recipe_gripe_summary.sql
--
-- gr661893: the gripe current-state summary's fold logs its LLM calls
-- under ``source = 'gripe_summary:rebuild'``
-- (``handlers/_gripe_summary.py``'s ``_LLM_SOURCE`` — ``route_small``'s
-- SMALL-tier "old summary + new ledger entry → new summary" rewrite a
-- stage-marked ``put`` or ``edit(mode='summary')`` fires), and no
-- task_recipes row covered it — the recipe-gap scan filed it as an
-- unknown task kind. It is the family's only source, so an exact row
-- covers it; a future sibling (``gripe_summary:<other>``) mints its own
-- row or the family gets a ``:``-terminated prefix row then.
--
-- Same demand shape as the ``chunk.summarize`` siblings
-- (``llm_summarize``, ``card_describe``, …): text in, compressed text
-- out, no lane. ``low`` consequence — the ledger stays the source of
-- truth and the rebuild's hard gate (anchors must exist in the ledger,
-- no raw locations) refuses a bad fold before it persists, so a weak
-- summary at worst sends the next agent to the comments.
--
-- Forward-only (ADR 0005). Idempotent (ON CONFLICT DO NOTHING — a
-- sibling slice that landed the same row first makes this a no-op
-- rather than a failure). Regenerate the baseline snapshot at release
-- time (ADR 0031): `scripts/bump` / `precis db dump-schema`.
INSERT INTO task_recipes (source, task_kind, lane_class, consequence, classify) VALUES
    ('gripe_summary:rebuild', 'chunk.summarize', 'none', 'low', false)
ON CONFLICT (source) DO NOTHING;

-- End of 0321_task_recipe_gripe_summary.sql
