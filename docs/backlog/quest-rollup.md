---
status: draft
pillar: platform
quest: qu540835
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

Reto, 2026-11-15 (chat, session specmagic): "add spec entries under their
relevant quest if we can, and gripes too, so most everything has a home. We
could also put churn (30 gripes filed, 20 fixed 12 open) in the summary
rollup at some level."

4. **Specs and gripes go under their relevant quest, not a bucket.** This
   amends ruling 2: "Gripe fixes" keeps only the gripes that resolve to no
   better quest (see Homes). The catch-all shrinks to what is truly
   unplaced.
5. **Churn is part of the rollup**: gripes filed, fixed and open per
   subtree, plus a whole-system line in the page header (see Churn).

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

## Homes: how a spec or gripe finds its quest

**Specs.** A live spec (`draft`, `ready`, `in-progress`; `idea`, `parked`
and `canonical` are left out) resolves to a quest by the first rule that
fires:
1. front matter `quest: quNNNNNN` (explicit, written by the backfill or by
   hand);
2. the quest of the thread file that lists it (`docs/backlog/threads/*.md`
   links; only 32 of 321 live specs, 2026-11-15);
3. a slug prefix that names its subject's quest (`pcb-`/`topo-`/`ewod-` →
   qu570323; `pathway-`/`kinetics-`/`slab-`/`catpath-`/`catalyst-`/… →
   qu500066; `hexfold-` → qu347422);
4. for a 3d-design spec, the one quest its text cites, if it cites exactly
   one (platform and memory specs cite science quests as examples, so the
   rule skips them);
5. its `pillar:`'s default quest (table below).

`scripts/quest-homes` (slice 0) implements these rules; its first dry run
(2026-11-15, specs only) placed 320 live specs: 233 by pillar, 65 by
prefix, 16 by thread, 5 by citation, 1 unplaced.

The project todo minted for the spec (ruling 3) `serves` that quest and
carries `spec:<slug>`, `backlog-spec` and `hold:no-dispatch`: it is the
spec's place in the tree, not a build order, so no lane picks it up (the
rollup shows it under 📄, not ⏸). The
front matter is the source; the todo's `serves` link follows it, and
`scripts/backlog-lint` flags drift between them.

| `pillar:` | live specs | default quest |
|---|---|---|
| platform | 74 | qu540835 Precis runs its own work |
| memory-graph | 75 | qu635085 Memory improvement |
| local-compute | 15 | qu635084 Maximize system utilization |
| quests | 19 | qu540835, unless rule 1 names the science quest it serves |
| 3d-design | 125 | qu686092 Precis designs things that get made (serves qu161906; qu570323 PCB boards moved under it) |
| personal | 7 | qu686093 Precis works for its own user |

**Gripes.** The stored home is a direct gripe → quest `serves` link; the
rollup reads that link (or, failing it, the fix todo's quest) and computes
nothing else. The rules below choose the link when a gripe is filed or
backfilled, first match wins:
1. the quest of its fix todo (`fixes`/`fixed-by`), when that todo is placed;
2. the quest of the spec it belongs to: a `spec:<slug>` tag, or a
   `cluster:<name>` equal to a live spec slug or thread name;
3. the one quest its title names ("quest qu164903 needs …");
4. a `cluster:` prefix naming the subject (`pcb-` → qu570323, `memory-`/
   `search-`/`claims-` → qu635085, `draft-`/`paper-`/`bib-` → qu459585,
   `neb-`/`pathway-`/`autocatpath-` → qu500066, …);
5. a `project:` with a subject quest (`memory`, `catpath`, `pcb`, …);
6. **Gripe fixes** (qu686227, under qu540835): a platform gripe (`precis`,
   `infra`, `release`, or no project tag) with no better home;
7. the catch-all.

The maps live in `scripts/quest-homes` (`CLUSTER_PREFIX_QUESTS`,
`PROJECT_QUESTS`) until slice 1 moves them into the gripe pipeline, which
then links each new gripe on file and each fix root todo to the same
quest. Inside any quest, gripe lines still group by `cluster:`.

## Churn

Per line, one more icon group, over a 7-day window by default:

| Icon | Rolls up |
|---|---|
| 📈 churn | `+filed −fixed · open`: gripes created in the window, gripes that entered `STATUS:done` in the window (`wontfix` counted apart in the hover), and live gripes now, all for gripes homed in the subtree |

Hover shows the same three numbers for 1 d / 7 d / 30 d and the done-todo
count for the window. The page header carries the whole-system line, e.g.
"Gripes 7 d: 30 filed · 20 fixed · 12 open · todos 41 done", with a
`?window=1d|7d|30d` switch. Close time is `ref_tags.created_at` of the
current terminal `STATUS:` tag (the axis replaces within the prefix, so
that row's timestamp is when the gripe closed); a reopened-then-closed
gripe counts once, at its last close. No schema change.

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
| 📈 churn | see Churn |

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
4. one query for churn: gripes created or closed in the 30-day window, with
   their home, bucketed per window in Python;
5. Python sums up the quest tree.

## Slices

0. **Homes backfill — applied 2026-11-15** (Reto: "apply").
   `scripts/quest-homes` dry run (TSV of each live spec and gripe → quest
   with the rule that fired), reviewed by Reto, then `--apply`: `quest:`
   front matter on every live spec, a project todo per spec, gripe → quest
   `serves` links, and the Gripe fixes quest qu686227. Re-runnable; new
   specs and gripes are picked up by a re-run until slices 1 and 5 make
   the placement automatic.
1. **Placement on file.** The gripe pipeline links each new gripe (and its
   fix root todo) to its home quest by the Homes rules, Gripe fixes only
   as the fallback; the rule maps move out of the script into the
   pipeline. Inside a quest, group lines by gripe `cluster:`/`thread:` tag.
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
   `draft`/`in-progress` spec with no project todo serving a quest, and a
   `quest:` line that disagrees with the todo's `serves`; the 📄 icon on
   the line.
6. **Churn.** The 📈 group and the header line (Churn); unit-tested on a
   seeded set of gripes with known create and close times.

## Settled (Reto, 2026-11-15)

- 3d-design and personal each got a dormant pillar quest (qu686092,
  qu686093), created the same day; qu570323 moved from qu161906 to
  qu686092.
- Churn window defaults to 7 d; `wontfix` is shown apart, not as fixed.
