---
status: ready
title: pcb — argue with the design: slice 2, the LLM acts on open arguments
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

## Left from slice 1

- `feature:*` handles resolve on submit but nothing in the render emits
  them yet (Edge_Cuts strokes carry no `%TO` attribute; `pcb_features`
  rows are not drawn as such). Emit `feature:outline` on the Edge_Cuts
  group once the outline is a `pcb_features` row on real designs.
- A part body is known only from `%TO.P` pad attributes — a part with no
  named pads has no click target. `%TO.C` on silk would fix it but changes
  gerber bytes (every fab-export byte assertion); decide deliberately.
- Acceptance criterion 1 (the cross-document click on `ewod-dogfood-1` at
  1280×800, screenshot) is still owed: the repo has no JS test runner, so
  `static/pcb-argue.js` has no unit test and the `<object>` listener was
  not exercised in a browser by the shipping session. Manual check: open
  `/pcb/ewod-dogfood-1`, click an electrode pad, the HV507 body and a
  track; the box must read e.g. `ARR1.R3C4 U1 net:HV_RAIL`.
- `pcb-argue-backport-se.md` is now unblocked.

## Slice 2 — the LLM acts on the argument

An argument note is an instruction, not just a record: "so the llm can
fix problems". Slice 1 answers inline, once, with prose. A `pcb_argue`
job type reads open argument notes on a design, resolves their anchors,
and answers each in place — following `gr335242` comment 1's playbook,
which already classified the three canonical argument forms and is
kind-agnostic:

- **change request** → propose the edit (`origin='proposed'`, human
  accepts) — e.g. "R_BLEED should be bottom-side".
- **interference/geometry concern** → run the real machinery (DRC on
  the named pair, clearance probe, courtyard check) and answer with
  numbers — e.g. "these two pads look shorted".
- **verification question** → run what exists and answer HONESTLY when
  the question exceeds the model, naming the backlog item it waits on —
  never a confabulated yes.

Every answer lands as an `answer`/`decision` note anchored to the same
handles (`store.pcb_note_insert`, `re=` the question), so the argument
thread lives on the design. `precis.utils.notes.open_questions` over
`pcb_notes_list` is the work queue.

**Wiring this slice needs, none of it free** (the vet caught all four):
`PcbHandler`'s `KindSpec` must set `can_own_jobs=True` (it never does
today — defaults `False`; `handlers/figure.py` is the explicit-`True`
precedent) so the job can be parented on the design it argues about;
a params schema; an executor lane assignment; and a **prod
`service_config` row**, without which the job type ships dark and
never runs — that exact gap has bitten this project twice (disputes
conflict-search, and the nm→se `se_propose_atomic` rename).

**Non-goal, explicit (Reto):** no place or route button on the page.
Actions stay in the MCP/job surface; the page is for arguing, not
driving.
