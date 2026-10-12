---
status: ready
pillar: memory-graph
title: fisheye everywhere — the remainder: focus-page filters, the quest eye's gist and Roadmap split
prio: normal
model: opus
---

# fisheye everywhere — what is left after the ladder landed on every kind

## Shipped (2026-10-02 … 2026-10-12; `git log` is the record)

The eye ladder rungs (`fisheye+2hop`, `+recall`), the ring registry
(`utils/refeye.py::RING_GROUPS`), `extent=` on every kind's `get`
(dispatcher → `Handler.eye`, shared helper `handlers/_eye.py`; every kind
renders or raises `Unsupported` with one sentence — totality test
`tests/test_eye_everywhere.py`), the neighbourhood render on memory,
paper, todo, taxon, concept, component and the rest of the link kinds, the
browser focus page `/eye/<handle>` (`precis_web/routes/eye.py`, with the
handle-or-query box), and `precis-fisheye-help` with one worked example per
ring group. The `focus` verb and render→act loop stay in
`fisheye-level2.md`; the SVG graph page is `web-graph-navigation.md`.

2026-10-12: ring filters on the focus page (`?kind=<csv>&space=<csv>`,
URL as state, chips reflect it, linked handles carry it forward; the
param parsing is `precis_web/graph_filters.py`, shared with the
`web-graph-navigation.md` SVG page's slices-to-come). The same narrowing
rides the eye door itself — `args={'ring': {'kinds':…, 'space':…}}` on
`get(extent=…)` → `eye_render.RingFilter`, narrowing first hop, second
hop and `+recall`. The `Roadmap` served-by mix resolved by ranking:
`refeye.py::RING_GROUP_KIND_RANK` lists `quest` neighbours first, so a
hub with hundreds of serving papers can't hide its sub-quests. And the
quest eye opens with its gist — statement + rubric composed from
`refs.title`/`refs.meta` by `handlers/quest.py::quest_gist` — not the
logbook, which stays at `view='logbook'`.

## In scope (remaining)

2. **Human acceptance**: from a finding hub Reto reaches its evidence
   papers, its `measures` rows and the quest that cites it in three clicks
   and no SQL. Not machine-checkable; closes when Reto says so.

## Navigation (2026-10-06) — dive in, back out

Reto, 2026-10-06: a graph viewer where one can "conveniently dive into
the graph and back out again". The human navigation surface is
`web-graph-navigation.md` (status ready, Reto's rulings of 2026-10-06:
focus page `/graph/<kind>/<id>` beside the reader, cookie-held trail,
caps). Its slices 7–9 carry what that ask added — zoom-out to a container
via `refeye.py::UP_RELATIONS`, the came-from slot, peek and the keyboard
map. `/eye/<handle>` here stays the text ladder: it links to the
`/graph/` page of the same handle and back, and `UP_RELATIONS` (defined
there) also gives this page its one "up" link. Edge editing is
`eye-link-editor.md`, blocked by `web-graph-navigation`. A trail or
keyboard layer is not duplicated here.

## Explicitly NOT in scope

- Measures, numeric-conflict rules, `quest_mesh`, the extraction landing
  seam (`knowledge-mesh.md`).
- The `focus` verb (`fisheye-level2.md`); the SVG focus page, path
  finding, trail (`web-graph-navigation.md`).
- New storage, background jobs or a cached neighbourhood: the ladder is
  computed on read.
- Mirroring skills or memory files into the graph (`file-mirror.md`).

## Acceptance criteria

1. ~~`/eye/<handle>?kind=memory&space=repo-dev` narrows every ring to that
   kind and SPACE; the same URL without the params renders the full
   ring.~~ shipped 2026-10-12 — `tests/precis_web/test_eye_route.py`.
2. Item 2 above, by Reto's word.
3. ~~`get(kind='quest', id=qu202467, extent='fisheye+1hop')` lists the
   serving sub-quests in `Roadmap` before any paper, and opens with the
   quest's gist, not its logbook.~~ shipped 2026-10-12 —
   `tests/test_eye_render.py`.

## Open questions / decisions log

- **[decided 2026-10-12]** The `served-by` mix resolved by ranking, not a
  split: `refeye.py::RING_GROUP_KIND_RANK` orders `quest` neighbours
  first in `Roadmap`, so serving sub-quests precede papers under the
  per-label cap. If a "best numbers" group is ever wanted for the quest
  ring it lands as a `RING_GROUPS` entry, not a retune of this rank.
- **[decided 2026-10-12]** The quest eye's body is a composed gist
  (`handlers/quest.py::quest_gist`, statement + `rubric`/`demand`/`supply`
  from `refs.title`/`refs.meta`), not a stored chunk — a meta edit can't
  drift it, and no quest needed a backfill.
- **[decided 2026-10-12]** Ring filters ride the eye door
  (`args={'ring': …}` → `Handler.eye(ring=…)` → `eye_render.RingFilter`),
  so `/eye/<handle>` and the MCP `get` narrow identically rather than the
  page carrying a second filter path.
- **[open, non-blocking]** The `+recall` cap (k=8) and whether recall
  crosses kinds by default (same kind + finding). Built with both
  defaults and a 0.6 cosine-distance floor (`eye_render._RECALL_*`).
- **[decided 2026-10-02]** A second-hop group expands through the same
  call with a filter, `extent='fisheye+2hop', q='<kind>:<label>'`; `more()`
  only pages an over-long body.
- **[decided 2026-10-02]** `+recall` is a suffix on any rung, not a rung;
  draft and plan sections stop at `fisheye+1hop`.
- **[decided 2026-10-09]** `extent=` is routed by the dispatcher to
  `Handler.eye`, not threaded through sixty `get` signatures; the ladder
  labels on `view=` stay valid as the spelling it shipped on. A whole
  draft or plan is `Unsupported` (the eye is a section); codeless,
  file-backed and `tag` kinds are `Unsupported` with the reason.
