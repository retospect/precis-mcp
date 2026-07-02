-- 0046_requested_by_relation.sql
--
-- Derived-job lane (ADR 0044) — register the link relation that binds a
-- requesting todo to a derived compute job it waits on:
--   * `requested`   (requester todo → derived job)  ↔ `requested-by`
--
-- A derived job (DFT relax, and later PCB route / CAD mesh / draft
-- compile) parents on its *subject artifact* — the structure/draft/cad
-- ref — not on a todo, because it is idempotent, content-addressed,
-- cache-fillable build work with no human-steering loop (contrast the
-- intent lane: a job under a todo, which drives the rotation + the
-- `child-failed` bubble). When an intentful task (a planner tick, a
-- human) *asks for* such a build and wants to block on it, it links
-- `requested` → the job. Two consumers follow this edge:
--   * the `derived_job_succeeded` auto_check evaluator closes the
--     requester when the linked job reaches STATUS:succeeded;
--   * the failure-bubble (`handlers/_job_bubble.py`) tags each requester
--     `child-failed:<job_id>` on failure (the job's own parent is an
--     inert artifact, so the requester is the thing with an owner).
--
-- Asymmetric, each with an inverse so "what did this todo request?" and
-- "who requested this job?" both auto-mirror at read time. Links FK into
-- `relations`, and the `Relation` Literal + `_INVERSE_RELATIONS` map in
-- store/types.py are kept in sync with this seed (type-checkers catch a
-- typo'd `rel=` ahead of the FK).
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('requested',    FALSE, 'requested-by',
     'Source todo requested target derived job and waits on it.'),
    ('requested-by', FALSE, 'requested',
     'Source derived job was requested by target todo.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0046_requested_by_relation.sql
