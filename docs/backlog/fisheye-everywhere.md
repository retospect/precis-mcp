---
status: ready
pillar: memory-graph
title: fisheye everywhere — the eye ladder on every kind, rings for taxon/skill/memory, and a focus page for any handle
prio: high
model: opus
---

# fisheye everywhere — every node gets a neighbourhood

## Motivation / why

Reto's stated goal for the graph (2026-09-30): a richly linked mesh of small
typed nodes an LLM navigates efficiently, with an excellent fisheye view.
Today `view='fisheye'` / `'fisheye+1hop'` is live on draft, finding and
quest (`precis-fisheye-help` says "partial rollout"); a memory, a
paper, a todo or a taxon has no neighbourhood render. The ring
set `utils/refeye.py::RING_RELATIONS` is `SEMANTIC ∪ CLAIM`, so kinds that
hang on other relations (`serves`, `specialises`, `instance-of`,
`has-prerequisite`, `part-of`) fall into the "Notes" bucket or show empty.
The ladder work was item 3 of `knowledge-mesh.md`, blocked two levels deep
on `measures-substrate` → `term-taxonomy`, and it depends on neither: it is
pure assembly of links, chunk gists and reading order that already exist.
Split out so it can ship first.

## In scope

Carved verbatim from `knowledge-mesh.md` in-scope 3 (now a pointer here):

1. **Eye ladder rungs.** Shipped 2026-10-02: `fisheye+2hop` and the
   `+recall` suffix (`utils/eye_render.py`; decisions below).
2. **`extent=` on every kind's `get`.** One argument selects the rung; a
   kind without chunks renders its card as the focus with links as rings.
   Every kind either supports the ladder or raises `Unsupported` with the
   reason in one sentence — no silent fallthrough to a bare chunk.
3. **Ring registry as a decision, not a consequence.** Shipped
   2026-10-02 as per-family ring groups (`utils/refeye.py::RING_GROUPS`).
4. **Generalised focus page.** `precis_web/draft_eyes.py` generalised to
   any handle: `/eye/<handle>` renders the same ladder in the browser.
   This is also the answer to "can we see the memory in a viewer": once
   `file-mirror.md` lands, the fisheye on a mirrored memory file is the
   viewer.
5. **Runtime doc.** `precis-fisheye-help` loses its partial-rollout
   section; `applies-to` becomes every kind; one worked example per ring
   group.

The `focus` verb and render→act loop stay in `fisheye-level2.md`; the
focus page here renders, it does not act.

## Explicitly NOT in scope

- Measures, numeric-conflict rules, `quest_mesh`, the extraction landing
  seam (`knowledge-mesh.md` keeps those).
- The `focus` verb (`fisheye-level2.md`).
- New storage, background jobs or a cached neighbourhood: the ladder is
  computed on read (ADR 0051 §6 stance).
- Mirroring skills or memory files into the graph (`file-mirror.md`);
  this item only renders whatever refs exist.
- A graph database or a materialised closure.

## Acceptance criteria

1. For every registered kind, `get(kind=K, id=<one live ref>, view=
   'fisheye')` returns a neighbourhood render or `Unsupported` with a
   one-sentence reason; a totality test walks the kind registry.
2. `get(kind='concept', id=C, view='fisheye+1hop')` shows its
   `has-prerequisite` edges and `component` its `contains` edges, each
   grouped under its family heading, as `quest` already does for `serves`
   (shipped: `handlers/quest.py::QuestHandler.get`,
   `tests/test_quest_fisheye.py`).
3. `get(kind='memory', id=M, extent='+recall')` lists the k nearest
   memory/finding chunks by embedding with their gist lines, k capped and
   documented.
4. `fisheye+2hop` renders the second hop as counts per (kind, relation)
   and `more()` expands one group; the render for a hub with >200
   second-hop edges stays under the response frame budget.
5. `/eye/<handle>` renders any kind that passes AC 1, with the same rung
   argument as the MCP view.
6. `precis-fisheye-help` no longer says "partial rollout"; `search(kind=
   'skill', q='see the neighbourhood of a quest')` returns it top.

## Target + blast radius

- `src/precis/utils/refeye.py`, `src/precis/utils/eye_render.py`
  (ladder, ring table, gist registry)
- every handler's `get` (`extent=` plumbing; one shared helper in
  `handlers/_numeric_ref.py` and the file-backed base)
- `src/precis_web/draft_eyes.py` → generalised eyes route
- `src/precis/data/skills/precis-fisheye-help.md`
- tests: `tests/test_kind_totality.py` (ladder totality), refeye tests,
  one web route test

## Open questions / decisions log

- **[decided 2026-09-30]** Split from `knowledge-mesh.md` in-scope 3
  because it depends on no measures work and is the goal's most visible
  surface. `knowledge-mesh.md` points here.
- **[open, non-blocking]** The `+recall` cap (start k=8) and whether recall
  crosses kinds by default (start: same kind + finding). Built with both
  defaults and a 0.6 cosine-distance floor (`eye_render._RECALL_*`).
- **[decided 2026-10-02]** AC 4's "`more()` expands one group" is not
  buildable as written: `more()` only pages an over-long body
  (`tools/core.py::more`) and knows no named group. A second-hop group
  expands through the same call with a filter, `view='fisheye+2hop',
  q='<kind>:<label>'`; the count line says so.
- **[decided 2026-10-02]** `+recall` is a suffix on any rung
  (`fisheye+1hop+recall`), not a rung of its own: similarity and edges are
  separate axes. A bare `+recall` means `fisheye+1hop+recall`. Draft and
  plan sections stop at `fisheye+1hop` (a ring entry is a ref; fisheye it
  to walk on) and refuse `+2hop`/`+recall` with that sentence.

## Pillar-review deltas (2026-09-30)

Folded in from the product-plan review's `web-graph-browse` (filed the
same day in a sibling tree, deleted as a duplicate of in-scope 4). The
browser focus page is the human graph-browse surface of `docs/roadmap.md`
pillar 1, so it carries three more requirements:

1. A search box on `/eye/` that resolves a handle or a query to a focus.
2. Ring filters by kind and by the `SPACE:` tag axis (`file-mirror.md`
   §"Pillar-review deltas"), so repo-dev and research neighbourhoods can
   be shown apart or together.
3. Human acceptance, in addition to AC 1–6: from a finding hub Reto
   reaches its evidence papers, its `measures` rows and the quest that
   cites it in three clicks and no SQL.
4. **The focus page is a curated human view, not an exhaustive walk**
   (Reto, 2026-10-01: an any-node-reaches-everything criterion "seems
   harsh"). From any node the page shows a chosen, ranked and bounded
   neighbourhood — its evidence, its measures, the quest that cites it —
   with each ring capped and ordered by the gist registry, and the rest
   behind `more()`. AC: for a node of each kind that has them, the three
   groups appear above the fold without SQL; a hub with >200 edges
   renders the same bounded page, never the full edge list.
5. **A quest's `served-by` block mixes sub-quests with papers and
   structures** (found 2026-10-03 when the quest ladder shipped).
   qu202467 (NO from exhaust → fertilizer N) has 484 serving papers, so
   its 8-row `served-by` cap can hide every serving sub-quest. Split the
   `Roadmap` ring by source kind, or rank sub-quests first. This is a
   `RING_GROUPS` decision, made when the measures pilot gives the quest
   ring a "best numbers" group.
