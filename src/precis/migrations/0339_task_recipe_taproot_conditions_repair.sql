-- 0339_task_recipe_taproot_conditions_repair.sql
--
-- gr696374: the conditions-repair sweep logs its LLM calls under
-- ``source = 'taproot:conditions-repair'``
-- (``taproot/conditions_repair.py``'s ``propose_conditions`` — one MEDIUM
-- call per hub that answers ``add`` / ``not_reported`` / ``ambiguous`` for
-- the must-state conditions the claim's sentence is missing), and no
-- task_recipes row covered it — the recipe-gap scan filed it as an unknown
-- task kind. The ``taproot:`` sources are individually enumerated
-- (migration 0278), not a prefix family, so this mints its own exact row.
--
-- Same demand shape as the ``claim.verify`` siblings (``chase:verify``,
-- ``taproot:reground-verify``): one claim judged against its evidence
-- paper, verdict plus paper-grounded output. Every insertion carries a
-- ``quote`` that must occur verbatim in the paper text, so the call's
-- unit of work is claim-vs-evidence verification even though its output
-- feeds a code-gated sentence rewrite. ``medium`` consequence — a bad
-- proposal that slipped post-validation would land wrong conditions in a
-- published claim sentence, unlike the low-consequence prose rewrites.
--
-- Forward-only (ADR 0005). Idempotent (ON CONFLICT DO NOTHING — a
-- sibling slice that landed the same row first makes this a no-op
-- rather than a failure). Regenerate the baseline snapshot at release
-- time (ADR 0031): `scripts/bump` / `precis db dump-schema`.
INSERT INTO task_recipes (source, task_kind, lane_class, consequence, classify) VALUES
    ('taproot:conditions-repair', 'claim.verify', 'none', 'medium', false)
ON CONFLICT (source) DO NOTHING;

-- End of 0339_task_recipe_taproot_conditions_repair.sql
