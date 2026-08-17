---
status: draft
title: Pathway viewer UX batch — per-pathway states, ordered TS, leaving species, keyboard nav
prio: normal
---

# Pathway viewer UX batch (user request 2026-08-17)

Reference page: `/refs/pathway/208430` (embedded viewer). TWO viewers
exist and should stay in parity where the feature applies:

- **Embedded**: `src/precis_web/templates/refs/pathway_detail.html.j2`
  (self-contained; graph + chips + states table live here).
- **Standalone catpath export**: `/Users/reto/catpath` —
  `src/autocatpath/render.py` / `report.py` (+ `naming.py`,
  tests `test_render.py`/`test_report.py`). Not yet explored this
  session — first step is mapping which features belong in which
  viewer (or shared).

## Requested features

1. **Chip-click filters the states table.** Selecting a candidate
   pathway chip should narrow the states table to only the states on
   that pathway (currently shows all states).
2. **Transition states in graph order.** TS rows (`EA=2.05` style)
   should appear in the order they occur along the reaction
   coordinate of the selected pathway, interleaved/ordered as in the
   graph — not whatever order they're stored in.
3. **Leaving species (`->H2O`) representation.** Many edges show
   `->H2O` (species allowed to leave the surface). User suspects these
   are "maybe not proper", and when a pathway is highlighted the graph
   does not show WHERE the H2O leaves. Investigate: (a) is the
   allowed-to-leave semantics correct in the underlying network, (b)
   render the departure point on the highlighted pathway (e.g. a
   marker/annotation on the edge where the species desorbs).
4. **Keyboard navigation.** With a pathway chip selected and the graph
   focused: ←/→ steps through states along the reaction coordinate
   (x-order); ↑/↓ switches between pathways (moves the selection
   within the pathway list). Mind focus handling / no page-scroll
   hijack when graph not focused.

## Also in the same UX bucket (small, separate surfaces)

- Drive `sort=created` option: `sort=recency` orders by `updated_at`
  (`store/_refs_ops.py::recent_refs`), so mass sweeps that stamp meta
  reshuffle the "recent" view — offer a created-order sort. (User
  asked "why is recent not proper" 2026-08-17; explained, fix
  optional.)
- r286 relax-run row on structure detail
  (`templates/structure/detail.html.j2`, cache-key row) still lacks a
  link to its result — parked user report, fold in if touching
  adjacent code.

## Acceptance criteria

- On `/refs/pathway/208430`: chip select → states table shows only
  that pathway's states with TS rows in coordinate order; highlighted
  pathway shows desorption markers; arrow keys navigate as specified.
- Standalone export either gains the same behaviors or the divergence
  is a recorded decision in this file before shipping.

## Target + blast radius

`src/precis_web/templates/refs/pathway_detail.html.j2` (+ its route
`src/precis_web/routes/refs.py` if data shape needs augmenting);
possibly `/Users/reto/catpath/src/autocatpath/render.py`/`report.py`
(separate repo — its own release flow, see memory `catpath-dev-deploy`).
