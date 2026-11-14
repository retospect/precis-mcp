---
status: draft
pillar: platform
---

# Quest rollup: one line per quest, rolled-up status down to the todos

Reto, 2026-11-14 (chat, session toasty-tumbling-breeze): "a quest based top
level rollup, active ones first. There's a short quest title, and it can
expand, to reveal sub quests etc until the todos show. Activity/status rolls
up […] A 'catchall' quest catches all the things that are not under any
quest. […] Alerts, ideally, show there, too, and any pending gripes and most
importantly actionable things. […] Use mouseovers and icons heavily."

Project todo: td681396 (serves qu540835; `hold:no-dispatch` until sliced).
This is the web nav's **Quests** entry (gr664411). It supersedes the Drive
tree layout (gr664408) and takes the todo-dashboard tree role (gr664410
keeps flat todo rows in Drive). `/work` (`todo/work_view.py::work_model`) is
the closest existing model and the base to extend; `/work` and the `/todo`
dashboard are retirement candidates once this ships.

## Rulings (Reto, 2026-11-14)

1. **Quests form a strict tree.** A quest `serves` at most one quest; a
   top-level todo `serves` at most one quest. Cross-references use
   `related-to`, which does not roll up. The two existing two-parent cases
   were fixed by hand the same day (qu202467 → qu161906 added;
   qu164903 → qu161906 and qu161909 → qu161908 removed).
2. **A "Gripe fixes" quest** under qu540835 ("Precis runs its own work")
   holds gripe work; every gripe-fix root todo serves it.
3. **Every backlog spec is visible under its quest** through a project todo
   named after the doc (pattern: td540836 "Dark factory … (docs/backlog/
   dark-factory.md)" serves qu540835). The rollup line carries a doc icon
   linking the spec.

## Data model (no schema change)

- quest → parent quest: `links.relation='serves'` (child is src).
- todo → todo: `refs.parent_id`; `NULL` (or a `folder` parent) is a root.
  No self-parent sentinel: it breaks recursive CTEs and hides real cycles.
- root todo → quest: `serves`; descendants inherit by walking `parent_id`
  (`asks_view.py::quest_of`).
- job → todo: `parent_id`. gripe ↔ fix todo: `fixes`/`fixed-by`.
  alert → subject: `related-to` (resolve subject → todo → quest).
- Direction todos (`part-of`/`contains` on a quest,
  `_todo_sql.py::direction_todo_sql`) are the quest's own planning tree:
  excluded from work counts.
- **Catch-all is computed, not a stored quest:** live root todos serving no
  quest, gripes with no fix todo under a quest, alerts whose subject traces
  to no quest. Prod 2026-11-14: 255 of 289 live roots (484 live todos) serve
  no quest, nearly all "Fix grNNN" roots — slice 1 drains them.

## The view

One line per node; click expands one level (htmx fragment), order
quest → sub-quests → root todos → child todos. Order: `active` quests first,
then `refs.prio`, then latest activity; `dormant`/`abandoned` collapsed at
the bottom; catch-all last. Test-fixture quests (`test-fixture`,
`dogfood-*` tags, qu600840) hidden.

Line: short title, then icon + count groups; hover on a group lists its
items, hover on an item shows status and links.

| Icon | Rolls up |
|---|---|
| ⚙ running | jobs `running`, todos `doing` under a live claim |
| ⏳ pending | open, doable todos |
| ⏸ paused | `paused`, `halt*`/`hold:*` tags, ancestor-parked |
| ⛔ blocked | `blocked`, `blocked-by` links, `waiting-for:` (nonzero only) |
| ✓ age | latest completion in the subtree ("3m") |
| 🙋 needs you | `asks_view.asks_model` classes, per subtree |
| 🐞 gripes | live gripes whose fix todo is in the subtree |
| 🚨 alerts | unacked alerts whose subject traces into the subtree |
| 🖥 resources | distinct lanes / hosts / models of live jobs underneath |
| 📄 spec | the backlog doc a project todo names |

Todo lines use the same groups for their own subtree. Each todo counts once
per line (the strict tree makes this automatic).

## Computation

Live per request, constant query count, ~30 s cache. Scale is small (54
quests, ~700 live todos):
1. one query: each live todo's bucket (reuse `routes/todo.py::_classify_row`
   semantics);
2. one recursive CTE: each todo's root and quest;
3. two or three set queries: live jobs, asks, gripes via `fixes`, alerts via
   `related-to`;
4. Python sums up the quest tree.

## Slices

1. **Gripe fixes quest + attachment.** Create the quest under qu540835; the
   gripe-fix pipeline links each new fix root `serves` it; one-time backfill
   of existing fix roots. Inside the quest, group lines by gripe
   `cluster:`/`thread:` tag.
2. **Single-parent rule at the write door.** `link(rel='serves')` from a
   quest (or a root todo) to a second quest refuses, with a next-step that
   offers the move (unlink old, link new). Lint/report for any existing
   violations.
3. **Rollup model.** Extend `work_model` (or a sibling) to the nested tree +
   the counts above + computed catch-all; unit-tested on a seeded tree.
4. **Quests page.** The nav entry; one-line rows, lazy expand, icons and
   hover detail; `/refs/quest` redirects here.
5. **Spec visibility.** Project-todo-per-backlog-spec convention in
   `AGENTS.md`/the backlog conventions; `scripts/backlog-lint` flags a
   `draft`/`in-progress` spec with no project todo serving a quest; the 📄
   icon on the line.
