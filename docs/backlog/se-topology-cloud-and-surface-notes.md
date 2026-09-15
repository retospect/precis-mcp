---
status: draft
title: se viewer — force-directed topology cloud + comment-on-selection → AI-rewritten interview notes
prio: medium
model: opus
---

# se viewer: topology cloud + surface comments

Reto, 2026-09-14: "the graph LR is confusing, can it be a cloud that
minimizes crossing links and is draggable, with mouseover details
(forces etc)? also I can select a surface in 3d but I can not comment on
it — and if i comment should it start a task or add a note?" Follow-up,
same day: "probably ai assisted rewritten note capturing intent
precisely?"

Two slices, independent, same page (`blocktree/detail3d.html.j2`).

## Slice 1 — topology cloud replaces the mermaid panel

Replace the `graph LR` mermaid panel with a force-directed node cloud:
force simulation naturally spreads nodes and minimizes crossings,
nodes are draggable (pinned while held, released on double-click),
hover shows connect/block detail.

- **No new endpoint.** `scene3d.json` already carries everything: shown
  blocks + ids (`Scene3D.shapes` leaf ids / `mermaid` node ids), the
  connect list (`Scene3D.connections` — `ConnLine` has `a_name`/`b_name`,
  `label` = `a.port—b.port (mechanism)`, `colour` = tension/compression/
  kinematic role, `witness_gap`). Add per-node payload (block level,
  envelope one-liner, mode) and per-edge force numbers **when a
  stability solve exists** (`precis_se.stability` self-stress /
  preload member forces; absent → hover shows role + gap only, honesty
  header style — never invent a number).
- **Renderer: small in-house force sim** (~100 lines: repulsion +
  spring + centering, SVG nodes/edges, pointer drag, `requestAnimationFrame`
  cooldown), same file pattern as `blocktree-3d.js`. Vendoring d3-force
  is the fallback if the home-grown sim fights hierarchy (house is
  CDN-free — vendor, never hotlink). Read the `dataviz` skill before
  building the panel (colors, tooltip, legend discipline).
- **Hierarchy carries over** from the subgraph work (shipped cf404b9e):
  opened parents render as a hull/halo behind their children (convex
  hull fill at low alpha), collapsed parents as single nodes — same
  `plan.shown` semantics, no new visibility logic.
- **Linked selection is preserved both ways** (block-id correspondence
  as today: 3D pick highlights the cloud node, cloud click selects in
  3D). The mermaid string in `Scene3D` stays for one release as the
  no-JS fallback (`<noscript>`/render-failure path), then dies.

## Slice 2 — comment on a 3D selection → interview note — RESOLVED (built)

Selection already resolves to a block (double-click pick →
`primaryPathOf` → block id). Add a comment box that appears on
selection; submitting it creates an **se interview note, not a task**
(decision, Reto 2026-09-14):

- The interrogation ledger is the home for design intent
  (`se_notes` via `precis.utils.notes.NoteSpec`: kinds
  question|answer|decision, `about=` anchors, `view='interview'`).
  Tasks stay downstream: the factory already mines open questions;
  a comment that needs work becomes a task when the loop picks the
  question up, not at capture time.
- **AI-assisted rewrite captures intent precisely**: raw comment →
  LLM rewrite (router small tier) into a crisp one-or-two-sentence
  note + a proposed `kind` (question vs decision — default question)
  + `about=` anchors (selected block name; face/feature detail folded
  into the body text, since anchors are block/measure names). The
  rewrite is shown inline for accept/edit before saving — the user's
  words are never silently replaced. Verbatim original is appended to
  the body as a `(verbatim: …)` trailer so nothing is lost.
- Stored with `origin='user'` (the intent is the user's; AI only
  phrased it). New web route `POST /se/{slug}/note` (auth'd, same
  session as the page) → the existing add_note op path — no new
  storage.
- Non-goal: free-floating 3D annotation pins (position-anchored
  comments). Anchors are blocks/measures; sub-block geometry lives in
  the note text until a real design needs more.

## Order

Slice 2 first (small, pure plumbing + one LLM call, immediate value);
slice 1 after (UI-heavy). Both testable without a browser except the
drag/hover layer — slice 1 needs one Playwright-style smoke test
(the gap gr338976 exposed: no client-JS render coverage at all).

Slice 2 landed 2026-09-14: `POST /se/{slug}/note` + `/note/rewrite`
(`routes/blocktree_view.py`), MEDIUM-tier rewrite with degrade-to-raw,
comment panel wired into `blocktree-3d.js`/`detail3d.html.j2`. Slice 1
is the remaining open work.
