-- 0056_plan_kind.sql
--
-- ADR 0051 §2b — the `plan` kind: a thread's reasoning outline. A
-- chunk-tree document mirroring `draft` almost 1:1 (same mutable-structure
-- chunk columns `handle`/`pos`/`parent_chunk_id`/`content_sha`/`retired_at`
-- + `chunk_events`, all added by 0031_draft_kind.sql), but a **distinct
-- kind** so it is NEVER exported as the deliverable (`corpus_role='none'`).
-- It is the thread's todo-list + notes, rendered *whole* every turn with
-- [open]/[wip]/done: status markers and a `▸` you-are-here cursor; it
-- survives tick exhaustion because it is store-backed.
--
-- Additive and behaviour-preserving: reuses the existing `chunks` /
-- `chunk_events` structure and `chunk_kinds` — NO new tables or columns.
-- Registers only the `kind` row + the `plan-of` project-binding relation
-- (the reasoning-outline sibling of the draft's `draft-of`; a project can
-- own both a draft and its plan without collision).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- 1. the ref kind ----------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('plan', FALSE, 'Plan',
     'A thread''s reasoning outline (ADR 0051 §2b) — a hierarchical '
     'todo-list + notes on the same chunk-tree substrate as a draft, '
     'addressed by pe<chunk_id>. Rendered whole with [open]/[wip]/done: '
     'status markers + a cursor; NEVER exported as a deliverable '
     '(corpus_role=none). One plan per project (plan-of link). '
     'See precis-overview.')
ON CONFLICT (slug) DO NOTHING;

-- 2. the project-binding relation (mirror store/types.py) ------------
INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('plan-of', FALSE, 'has-plan',
     'Source plan is the reasoning outline of target project (todo).'),
    ('has-plan', FALSE, 'plan-of',
     'Source project (todo) has target plan as its reasoning outline.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0056_plan_kind.sql
