---
id: precis-pcb-design-flow-help
family: pcb
title: precis — pcb design flow: requirements to an orderable board
summary: the steps around the netlist — requirement nodes, architecture and part-selection records in graph memory, the budget convention, where the BOM lives, decisive facts, the mech/env checklist, review + fab. Covers requirements, architecture decisions, shortlists, datasheets, budgets, component values, board script, layout, checklist, fab.
answers:
  - where does the power budget live?
  - how do I record why I chose a part?
  - what are the steps from requirements to an orderable board?
  - where does the bill of materials live — script, pcb, or draft?
  - what is a selection record and how do I write one?
applies-to: design work around put (kind='pcb'); memory (selection records); draft (published tables)
status: active
tags: [design]
kinds: [pcb, part, memory]
---

# precis-pcb-design-flow-help — requirements to an orderable board

The pcb skills start at the netlist. This one covers the work before and
after it: every step's artifact has a declared home, so nothing is
improvised. Design intent lives in **graph memory**: requirement nodes
link `served-by` decision nodes; decisions link `derived-from` the facts
and chunks they rest on; every node is anchored to refdes/net handles.

Owner skills named per step below own their verb detail — this skill is
the router. Still landing (later slices of the same item): consigned BOM
lines and the budget `publish` helper.

## The flow — eleven steps, each artifact homed

| # | step | artifact | home (kind) | owner skill |
|---|------|----------|-------------|-------------|
| 1 | requirements | one node per requirement, with the number where one exists ("night ≥ 12 h at 9 µA") | memory, tag `requirement`, linked to the project todo | this skill |
| 2 | architecture | one decision per system choice (radio PHY, power topology, storage) | memory selection record, `role: architecture/<x>` | this skill |
| 3 | shortlist per role | a selection record with 2–4 candidates, `status: open` | memory selection record | [[precis-part-select-help]] |
| 4 | datasheets | each candidate's sheet pulled, facts extracted | `datasheet` + part facts | [[precis-datasheet-help]] |
| 5 | decisive facts | a fact one chunk proves cites that chunk (`part:C#~pin`, `datasheet:<slug>~N`); a fact reused across designs becomes a finding | finding / part facts | [[precis-finding-help]] |
| 6 | budgets | energy (then link, thermal): the model and its asserted margins | workspace script; tables + figure published to the design draft; per-net `est_current_a` on the pcb | this skill |
| 7 | component values | externals and straps from facts, each citing its fact | board script | pcb-datasheet-review |
| 8 | schematic | parts + nets; each part line cites its selection record `# me<id> (why)` | board script → pcb | [[precis-pcb-help]], [[precis-workspace-help]] |
| 9 | layout | topo place → topo route → post-route widening from `est_current_a` → check surface | pcb | [[precis-pcb-topo-help]], [[precis-pcb-check-help]] |
| 10 | mechanical / environment | checklist `pcb-mech-env` answered | checklist on the pcb | this skill |
| 11 | review + fab | `op='review'`, then exports, consigned lines separate | pcb | [[precis-pcb-review-help]], [[precis-pcb-fab-help]] |

## Mechanical / environment review (step 10)

To activate `pcb-mech-env`, declare the board's flex process
(`board.meta.process = 'flex-…'`), `fold_lines`, or the applicable fab
features: `bend_zone`, `stiffener`, `coverlay_opening` or `tape_mask`
(coating keep-off). These declarations are authorable through the MCP today.

Board metadata keys `flex`, `coating`, `enclosure`, `environment`, `outdoor`
and `wet` are also read if present and trigger the checklist by presence,
including incomplete declarations. They cannot be set through the MCP yet
(gr524298). A plain rigid board gets only `pcb-tapeout` by default.

`put(kind='pcb', id=<slug>, args={'op':'review', 'page_size':60})` works
both checklists through the same review job; re-run if the summary reports
items left. Read `get(kind='checklist', id='pcb-mech-env', target='pcb:<slug>')`.
Its eight judgment items cover bend zones, stiffeners, coverlay clearances,
coating keep-off, access after coating, mounting/strain relief, every part's
temperature rating and condensation/ingress. Coverlay openings must be
≥ pad + 0.1 mm and ≥ 0.15 mm from the next trace.

Unrepresented or missing geometry answers `not checkable (gr520329)`;
it never becomes a pass. See [[precis-pcb-review-help]] for verdicts,
evidence and re-runs.

## The selection record — why a part was chosen

One **memory node** per role per design, tagged `decision`, `selection`
and `project:<slug>`:

```
role: <what it does in the circuit>         e.g. energy storage, 12 h night
criteria: <ranked, each with its threshold> e.g. ESR < 1 ohm, leakage < 10 uA
candidates:
  - <MPN> (<C# or 'consign'>): chosen | rejected — <one-line verdict> [<fact handles>]
pick: <MPN> — <why, citing fact handles>
status: open | decided | provisional
```

The record's links carry its evidence:

- `derived-from` → each datasheet chunk, part fact or finding it rests
  on. There can be many — this is what a single-cited `finding` cannot
  hold.
- `serves` → the requirement node(s) from step 1.
- `selects` → `part:<C#>` (or the `component` ref of a consigned part).
- `rejects` → each rejected part, with `meta={'reason': '<why rejected>'}`.
- an anchor link → `pcb:<slug>~#<refdes>` once the part is placed.

Write the pick with `link(kind='memory', id=<record>, target='part:C123',
rel='selects')`; rejects use `rel='rejects', meta={'reason': '<why>'}`.
The board script's part line cites the record: `# me<id> (why)`.
`get(kind='pcb', id='<board>#<REFDES>')` lists current decisions linked
to that instance's catalog part: selects first, then rejects with reasons.
Consigned component placement is a later slice. Existing interim
`related-to` edges need an operator-approved rewrite to appear here.

## Managing a record over the design's life

A record is edited in place while `open`. Once `decided` it changes only
through these moves, and no record is ever deleted:

- **Add or drop a candidate while open.** Edit the `candidates:` block
  and its `rejects` link; each verdict line keeps its fact handles.
- **Re-select** (better part, failed review, EOL/NRND/out of stock):
  write a *new* record for the same role whose `pick` is the new part;
  link it `supersedes` → the old record; set the old record to
  `status: superseded`. The new record copies the old rejects whose
  reasons still hold and turns the old pick into a `rejects` link
  whose `meta.reason` says why it fell ("EOL per Digi-Key 2026-11-02").
  Update the script's `# me<id>` cite in the same change. `#REFDES`
  omits the superseded record; its history stays one `supersedes` hop back.
- **Keep rejects.** A rejected part is never unlinked — the reason is the
  reusable knowledge. A rejection later proved wrong is struck through in
  the body, not deleted.
- **Reuse across designs.** A later design's record may `derived-from`
  an earlier record's rejects instead of re-arguing them. The
  `selects`/`rejects` relations are scoped to part selection — architecture
  decisions (step 2) use `derived-from` and `serves` only.

## The budget convention

The energy (then link, thermal) budget is **code in the design's
workspace script folder**, not a doc: the model runs and *asserts* its
margins — `assert night_hours >= 12 * 1.25`. Every input constant carries
a cite-the-DB comment — `I_SLEEP = 1.9e-6  # pc5354056 (nRF52833 System
ON, RTC)`. A run does three things:

1. prints the tables;
2. `put`s them as **table chunks** into the design draft — the draft's
   graph figure re-renders from them (a figure renders only from draft
   table chunks, which is why the model can't live in the draft);
3. writes each rail's `est_current_a` onto the pcb's nets — the hand-off
   to post-route widening, so budget and copper can't disagree.

A failing assert blocks the publish.

## Where the BOM lives

Three copies, one editable:

- **Source** — the board script's part lines. This is the only copy you
  edit.
- **Queryable** — the pcb design: `get(kind='pcb', id=<slug>, view='bom')`.
- **Published** — a draft table regenerated from `view='bom'`, never
  hand-edited.

## Decisive facts (step 5)

A fact **one chunk proves** cites that chunk — `part:C#~<pin>`,
`datasheet:<slug>~<chunk N>` — never a bare prose claim. A fact reused
across designs graduates to a `finding` (`cited_in` = that chunk); a
design-local fact stays a `derived-from` link on the selection record.
