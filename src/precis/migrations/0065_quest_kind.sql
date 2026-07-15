-- 0065_quest_kind.sql
--
-- The `quest` kind — the striving above the work (docs/proposals/quest-layer.md).
-- A quest is a PERPETUAL, UNACHIEVABLE striving (the medieval Grail sense): you
-- never file it `done`, you strive toward it and it pulls subtasks + knowledge
-- acquisition into its service. It is the ONLY new kind in the model — the
-- achievable structure beneath it stays ordinary todos/projects, marked as
-- serving the quest by a link.
--
-- Numeric-id ref (like memory/concept/gripe): `title` = the striving statement
-- (+ success criteria); `meta` carries `priority` (striving weight) + `horizon`;
-- the `STATUS:` tag carries the lifecycle `active | dormant | abandoned` (there is
-- no `done` — that would delete the "% done" axis as the wrong measure). The
-- statement is emitted as the reused `card_combined` chunk (ord=-1, no new
-- chunk_kind) so a quest IS a vector in the corpus manifold — the substrate the
-- alignment floor + reading calibration consume for free. corpus_role is
-- authored/'none' (never cited as evidence) — enforced by the handler/export
-- guard, not here.
--
-- Two records hang off a quest (slice 1 ships the first):
--   * LOGBOOK — append-only `quest_log` chunks (the gripe body+comment pattern):
--     a WORM, dated ledger of what happened. Lightly typed entries (note ·
--     observation · hypothesis · result · decision · dead-end · milestone ·
--     reflection · cost) in `meta.entry_type` + a `meta.by` field. A DEED is just
--     a `milestone` entry, so the deed ledger is a filtered view of the log. The
--     log is ALSO the cost ledger — spend lives in `meta.cost` on entries, so the
--     TOTE is a query over the dated log, no separate cost store.
--   * DOSSIER — a `draft` the quest owns (arrives with the research loop, slice 4;
--     not registered here).
--
-- Also registers the `serves` link relation (kept in sync with the `Relation`
-- Literal + `_INVERSE_RELATIONS` in store/types.py):
--   serves ↔ served-by  — X serves quest; quest served-by X (asymmetric,
--     auto-mirrored). Covers project/todo/concept/paper/job/draft/structure and
--     SUB-QUEST → quest (a DAG of strivings above the todo tree of deeds).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('quest', TRUE, 'Quest',
     'A perpetual, unachievable striving (the medieval Grail sense) that pulls '
     'subtasks and knowledge acquisition into its service. Never `done` — '
     'lifecycle is active/dormant/abandoned. Achievable goals beneath it are '
     'ordinary todos/projects marked `serves`. Progress is a ledger of deeds, '
     'not a percentage. See docs/proposals/quest-layer.md.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('serves',     FALSE, 'served-by',
     'Source (project/todo/concept/paper/job/draft/structure/sub-quest) is in '
     'the service of the target quest — the striving DAG above the todo tree.'),
    ('served-by',  FALSE, 'serves',
     'Source quest is served by the target work/knowledge node.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('quest_log', FALSE,
     'Quest logbook entry — a WORM, dated, append-only ledger row (note / '
     'observation / hypothesis / result / decision / dead-end / milestone / '
     'reflection / cost) carrying entry_type + by + optional cost in meta. A '
     'milestone entry is a deed; a cost entry feeds the tote.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0065_quest_kind.sql
