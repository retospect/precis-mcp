---
status: ready
title: fisheye everywhere — the eye ladder on every kind, rings for taxon/skill/memory, and a focus page for any handle
prio: high
model: opus
---

# fisheye everywhere — every node gets a neighbourhood

## Motivation / why

Reto's stated goal for the graph (2026-09-30): a richly linked mesh of small
typed nodes an LLM navigates efficiently, with an excellent fisheye view.
Today `view='fisheye'` / `'fisheye+1hop'` is live on draft and finding
chunks only (`precis-fisheye-help` says "partial rollout"); a memory, a
paper, a quest, a todo or a taxon has no neighbourhood render. The ring
set `utils/refeye.py::RING_RELATIONS` is `SEMANTIC ∪ CLAIM`, so kinds that
hang on other relations (`serves`, `specialises`, `instance-of`,
`has-prerequisite`, `part-of`) fall into the "Notes" bucket or show empty.
The ladder work was item 3 of `knowledge-mesh.md`, blocked two levels deep
on `measures-substrate` → `term-taxonomy`, and it depends on neither: it is
pure assembly of links, chunk gists and reading order that already exist.
Split out so it can ship first.

## In scope

Carved verbatim from `knowledge-mesh.md` in-scope 3 (now a pointer here):

1. **Eye ladder rungs.** `+recall` (semantic neighbours from the chunk
   embedding, capped) and `fisheye+2hop` (second hop as counts per kind per
   relation, expandable by `more()`), on top of the existing
   `fisheye` / `fisheye+1hop`.
2. **`extent=` on every kind's `get`.** One argument selects the rung; a
   kind without chunks renders its card as the focus with links as rings.
   Every kind either supports the ladder or raises `Unsupported` with the
   reason in one sentence — no silent fallthrough to a bare chunk.
3. **Ring registry as a decision, not a consequence.** `RING_RELATIONS`
   becomes a per-kind-group table: `ROADMAP_RELATIONS` (`serves`), taxon
   relations (`specialises`, `instance-of`; lands with term-taxonomy),
   concept relations (`has-prerequisite`, `analogy-of`, `contrasts-with`),
   component relations (`contains`, `part-of`, `made-of`), argument
   relations (`entails`, `qualifies`, `derived-from`), and the mirrored
   file kinds' `related-to` (lands with `file-mirror.md`). The
   gist-per-kind registry test pins every kind to a gist renderer.
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
2. `get(kind='quest', id=Q, view='fisheye+1hop')` shows the quests Q
   serves and the ones serving it, grouped under `serves`; the same for
   `concept` over `has-prerequisite` and `component` over `contains`.
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
  crosses kinds by default (start: same kind + finding).

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
