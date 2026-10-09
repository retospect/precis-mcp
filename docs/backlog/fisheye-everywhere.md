---
status: ready
pillar: memory-graph
title: fisheye everywhere — the remainder: focus-page filters, the quest eye's gist and Roadmap split
prio: normal
model: opus
---

# fisheye everywhere — what is left after the ladder landed on every kind

## Shipped (2026-10-02 … 2026-10-09; `git log` is the record)

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

## In scope (remaining)

1. **Ring filters on the focus page** by kind and by the `SPACE:` tag axis
   (`file-mirror.md` §"Pillar-review deltas"), so repo-dev and research
   neighbourhoods can be shown apart or together. Query params, URL as
   state, chips reflect it — the same shape `web-graph-navigation.md`
   slice 4 gives the SVG page; share the filter parsing with it.
2. **Human acceptance**: from a finding hub Reto reaches its evidence
   papers, its `measures` rows and the quest that cites it in three clicks
   and no SQL. Not machine-checkable; closes when Reto says so.
3. **A quest's `served-by` block mixes sub-quests with papers and
   structures** (found 2026-10-03). qu202467 has 484 serving papers, so
   the 8-row `served-by` cap can hide every serving sub-quest. Split the
   `Roadmap` ring by source kind, or rank sub-quests first. A
   `RING_GROUPS` decision, made when the measures pilot gives the quest
   ring a "best numbers" group.
4. **The quest ladder's fisheye body is the logbook** (dogfood 2026-10-04).
   `get(kind='quest', id=202467, extent='fisheye+1hop')` puts about 30
   lines above the ring: the statement, then the logbook entries. A
   fisheye should open with the node's gist (statement and rubric) and
   leave the logbook to `view='logbook'`. The renderer takes the body
   from the quest's chunks; a quest needs a one-chunk gist for the eye,
   the same way a finding has its claim.

## Explicitly NOT in scope

- Measures, numeric-conflict rules, `quest_mesh`, the extraction landing
  seam (`knowledge-mesh.md`).
- The `focus` verb (`fisheye-level2.md`); the SVG focus page, path
  finding, trail (`web-graph-navigation.md`).
- New storage, background jobs or a cached neighbourhood: the ladder is
  computed on read.
- Mirroring skills or memory files into the graph (`file-mirror.md`).

## Acceptance criteria

1. `/eye/<handle>?kind=memory&space=repo-dev` narrows every ring to that
   kind and SPACE; the same URL without the params renders the full ring.
2. Item 2 above, by Reto's word.
3. `get(kind='quest', id=qu202467, extent='fisheye+1hop')` lists the
   serving sub-quests in `Roadmap` before any paper, and opens with the
   quest's gist, not its logbook.

## Target + blast radius

- `src/precis_web/routes/eye.py` (filters), `src/precis/utils/eye_render.py`
  (`_first_hop` filter hook; quest gist body), `utils/refeye.py::RING_GROUPS`
  (Roadmap split), `handlers/quest.py` (a gist chunk), skill
  `precis-fisheye-help`.

## Open questions / decisions log

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
