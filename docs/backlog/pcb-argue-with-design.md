---
status: ready
title: pcb — argue with the design: remainder (decisions + ops owed)
prio: high
model: opus
pillar: 3d-design
---

# pcb: argue with the design — remainder

Slice 1 shipped (`feat(pcb): argue with the design …`): one always-present
text box on `/pcb/{slug}`; clicking a pad / part body / track in the fab
render (`render_fab_svg` stamps `data-handle`, the host page listens
inside the `<object>`'s own document) inserts the handle at the caret;
`POST /pcb/{slug}/note` resolves every clicked handle (an unknown one is a
400 quoting the valid roster), stores the text verbatim as a `question`
in `pcb_notes` (migration 0193, the `se_notes` shape) with the handles it
names in `about`, asks the model once through `route()` with the
resolved context and stores the reply as an `answer` re the question;
`get(kind='pcb', view='notes')` reads the ledger with dangling anchors
reported. The grammar and resolver live in `src/precis/pcb/argue.py` —
other items (`pcb-keepout-does-not-bind.md`, `pcb-platform.md`) that cite
"the anchor grammar" mean that module:

| clicked | handle | resolves to |
|---|---|---|
| part body (extent of its pads) | `U_TEMP` | `pcb_instances.refdes` |
| pad (incl. a generated array cell) | `U_TEMP.3`, `ARR1.R3C4` | refdes + pin |
| net (track / via / pour) | `net:HV_RAIL` | `pcb_nets.name` |
| board feature | `feature:outline` | `pcb_features.ftype` |
| nothing (whole-design argument) | *(no handle)* | the design |

Slice 2 shipped (`feat(pcb): argue slice 2 - pcb_argue job acts on open
notes`): the `pcb_argue` job type (`src/precis/workers/job_types/
pcb_argue.py`, `job_inproc`, parent the design it argues about —
`PcbHandler` `KindSpec.can_own_jobs=True`) reads open `question` notes
(`open_questions` over `pcb_notes_list`), resolves their anchors through
`argue.resolve` (object handles against the same selectables catalogue
the page builds), runs the real machinery once per run — a live
geometric DRC run via `PcbHandler._drc_run`, `eyes.evaluate_measures`,
part-pair centroid distances — and stores one `answer` note
(`origin='proposed'`, `re=` the question, same `about`) per question via
`argue.act`'s playbook prompt (gr335242 comment 1 verbatim: propose
edits for change requests, numbers for interference concerns, an honest
"not modelled, waits on <item>" for what the evidence can't settle).
`argue.vitals` is the shared vitals block the page and the job both
quote. Mint door: `put(kind='job', job_type='pcb_argue',
parent_id=<pcb ref>, params={'pcb_ref_id': N, 'limit'?, 'question'?})`.
The Edge_Cuts group of `render_fab_svg` now carries
`data-handle="feature:outline"` whenever the board's outline is a
`pcb_features` row — the first `feature:*` handle the render emits.

## Left

- **prod `service_config` row for `pcb_argue`** — the doc's fourth
  wiring item, owed to prod ops (the same gap that bit disputes
  conflict-search and the `se_propose_atomic` rename). Without it the
  job type has no console record and no enablement story.
- A part body is known only from `%TO.P` pad attributes — a part with no
  named pads has no click target. `%TO.C` on silk would fix it but changes
  gerber bytes (every fab-export byte assertion); decide deliberately.
- Acceptance criterion 1 (the cross-document click on `ewod-dogfood-1` at
  1280×800, screenshot) is still owed: the repo has no JS test runner, so
  `static/pcb-argue.js` has no unit test and the `<object>` listener was
  not exercised in a browser by the shipping session. Manual check: open
  `/pcb/ewod-dogfood-1`, click an electrode pad, the HV507 body and a
  track; the box must read e.g. `ARR1.R3C4 U1 net:HV_RAIL`.
- `pcb-argue-backport-se.md`'s box shipped 2026-10-15 — one always-present
  argue box on `/se/{slug}` (`#bt3d-argue`), a block pick inserts the
  handle at the caret, `about[]` carries every clicked anchor still in
  the text, se keeps its rewrite step. That file now holds only the
  gr335242 remainder (playbook, surface anchors/markers, stick figure).

## Left from gr335242 (se/nm viewer, rounds 1/2a shipped)

Shipped: round 1 (reader, part isolation, hierarchy levels) and round 2a
(three-cad-viewer 3D route, per-subtree level overrides). Still unbuilt,
and shared with this item's mechanism (anchors, notes ledger, job):

- **Argue-with-points** — click a point on the SVG/3D render, resolve it
  to an exact anchor (block + surface point) via the cad kernel's analytic
  ray probe (no mesh picking), take a natural-language argument, store it
  as `add_note(kind='question', about=[anchor])`, optionally enqueue a
  design-task job carrying anchor + prompt. New parts: the SVG→anchor
  picker and the job type.
- **Notes layer** — anchored notes shown in the viewer, level-aware
  (notes today are level-specific by construction).
- **Pre-solve artefact** — the stick-figure artefact an argument can
  attach to before a solve exists.
- Face-picking blocker: curved primitives tessellate into per-triangle
  "faces"; a clean fix needs `precis.cad.tessellate` to return a
  per-triangle analytic-facet id.

Who: agent-code for the picker/job type; the pcb handle grammar
(`src/precis/pcb/argue.py`) is the model to mirror for se/nm.

**Non-goal, explicit (Reto):** no place or route button on the page.
Actions stay in the MCP/job surface; the page is for arguing, not
driving.
