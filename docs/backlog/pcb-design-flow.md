---
status: ready
title: "pcb design flow: requirements-to-fab skill, part selection record, budgets, consigned BOM lines, mech/coating checklist"
pillar: 3d-design
prio: high
model: opus
---

# pcb design flow — from requirements to an orderable board

Source gripes: gr519925 (no design-procedure skill, no power-budget
convention, no mech/coating checklist, no consign BOM field), gr519924
(no place to record why a part was chosen). Both came from the TAZ
leaf-node dogfood, td485252 / td519926, 2026-10-26.

## Motivation / why

The pcb skills start at the netlist. `precis-pcb-help` "Canonical
end-to-end" opens with "1. **Pick parts**" and then "2. **Capture the
netlist**". Nothing covers the work before that, which is most of the
design:

```
requirements -> architecture -> shortlist per role -> datasheets
-> decisive facts -> budgets -> component values -> board script -> layout -> fab
```

On td485252 the agent improvised a home for every artifact and Reto had
to ask:

- **Decisions.** Part choices (nRF52832 -> 52833 for Coded PHY; BQ25570
  -> BQ25504; four supercap candidates) ended up as prose in todo
  comments. That prose has no candidates, criteria or decisive chunk, and
  the next design can't find it.
- **Supercap record.** The interim "finding tagged decision" for the
  supercap was refused: `put(kind='finding')` needs one `cited_in` chunk,
  and a trade-off rests on several sheets. The agent cited one chunk to
  get fi520367 stored.
- **BOM home.** Where the BOM lives (script, pcb, or draft) is settled in
  no skill.
- **Energy budget.** The budget had no home. A draft figure renders only
  from draft table chunks, so the model can't live in the draft.
- **Consigned parts.** The supercap and the KXOB25 cells (JLC stock 0) had
  no BOM representation. `bom_csv` writes LCSC blank and `view='fab'`
  warns "non-assemblable".
- **Mechanical and coating constraints.** Flex bend, stiffener, coverlay
  openings and the parylene mask had no checklist.

## What already exists (do not rebuild)

- **Shortlist → recorded selection** is slice 8 of
  [pcb-datasheet-review](pcb-datasheet-review.md). That item ruled
  (2026-10-13) that design intent lives in **graph memory**:
  - requirement nodes link `served-by` decision nodes;
  - decisions link `derived-from` the facts and chunks they rest on;
  - every node is anchored to refdes/net handles.

  This item fixes the record's concrete shape and the skill that writes
  it. That item keeps the lifecycle attribute and the staleness on a
  lifecycle or stock change.
- **Part facts** (`datasheet_facts`: pin table, required externals, strap
  tables, page-cited) are owned by
  [datasheet-facts-mesh](datasheet-facts-mesh.md). Building circuits from
  them is pcb-datasheet-review slice 9.
- **Part brief** (what a part is, as a one-liner plus a sourced
  ~200-word brief) is gr520243. This item cites briefs; it does not
  produce them.
- **The board script.** It is the source of truth for the netlist
  ([pcb-workspace-code](pcb-workspace-code.md)). The cite-the-DB comment
  convention is `# <handle> (<why>)`, documented in
  `precis-workspace-help` §Cite the DB. Its pins are classified at first
  use (gr520810): `net.tie`/`b.wire` refuse a pin or slot without an
  `etype`, that etype's facts (`fmax_hz`; `max_current_a`+`max_voltage_v`
  on `power_in`/`power_out`) and a provenance key — `source=` is where
  step 5's chunk handles (`part:C#~pin`, `datasheet:<slug>~N`) land on
  the pin, `assume=` records the honest guess when no fact exists.
- **Layout.** The topo router is the only router; vroute was deleted
  2026-10-25 (gr515107), and the topo realiser is pcb-topo-place slice 5.
  There are no pours: copper comes from traces widened by net property,
  per [pcb-graph-routing-fattens-traces](pcb-graph-routing-fattens-traces.md).
  `resolve_track_rules` already maps a net's `est_current_a` to an
  IPC-2221 width.

  **Topo router state** (read-only design review of `worktree-topo-s5`,
  2026-10-26). Conclusions are copied here because the review file is
  not durable. The fixes belong to
  [pcb-topo-place](pcb-topo-place.md), not to this item.
  - The method is sound: a CDT mesh, wires as crossed-edge sequences,
    per-edge order lists, layer assignment, then geometry. This is the
    SURF / rubber-band shape.
  - Three design pieces are missing:
    - spacing-aware realisation: the realiser emits the taut sketch
      verbatim, so wires pass through pad corners and coincide;
    - capacity/congestion feedback: `capacity_mm`/`gap_cap` are computed
      and nothing reads them;
    - crossing resolution beyond one greedy outer-layer flip: no
      re-thread, no inner layers, no rip-up.
  - Bench symptoms:
    - nano 0/64 is a mesh-recovery crash (`MeshConflictError` on a
      collinear header row) that the job swallows as "unrouted";
    - esp32c3's 80–145 DRC errors come from 0.000 mm clearances and vias
      minted on pad-ring edges;
    - ewod-replay moves are a placer gap: a fixed via is not a net anchor,
      and the via exemption is per part, not per pad.
  - Ranked plan:
    1. mesh robustness and loud failures;
    2. spacing-aware realisation (spoke offsets);
    3. via siting with keepout;
    4. crossing resolution (inner layers, re-thread, bounded rip-up);
    5. fixed vias as placer anchors;
    6. capacity cost.

  Pours stay ruled out. Consequence for this flow: until fixes 1–4 land,
  step 9 ends at a topo placement plus the check surface. The skill says
  so and does not tell agents to hand-route or pour. The `est_current_a`
  hand-off from step 6 is still written, so post-route widening picks it
  up when routing produces copper.
- **Flex fab layers and bend zones** (coverlay, stiffener, coating
  keep-off, documentation layer) are gr520329 and
  [pcb-flexboard](pcb-flexboard.md). This item adds only the checklist
  that asks whether they were declared.
- **Datasheet ingest for non-LCSC parts** is gr519849.

## The flow and each step's artifact

This table is the core of the new skill. Every step names the kind its
artifact lives in, so nothing is improvised.

| # | step | artifact | home (kind) | owner skill |
|---|---|---|---|---|
| 1 | requirements | one node per requirement, with a number where one exists ("night ≥ 12 h at 9 µA") | memory, tag `requirement`, linked to the project todo | this skill |
| 2 | architecture | one decision per system choice (radio PHY, power topology, storage) | memory selection record, `role: architecture/<x>` | this skill |
| 3 | shortlist per role | a selection record with 2–4 candidates, `status: open` | memory selection record | `precis-part-select-help` |
| 4 | datasheets | each candidate's sheet is pulled and has facts | `datasheet` + part facts | `precis-datasheet-help` |
| 5 | decisive facts | a fact one chunk proves cites that chunk (`part:C#~pin`, `datasheet:<slug>~N`); a fact reused across designs becomes a finding (`cited_in` = that chunk) | finding / part facts | `precis-finding-help` |
| 6 | budgets | energy (then link, thermal): the model and its asserted margins | workspace script; tables + figure published to the design draft; per-net `est_current_a` set on the pcb | this skill |
| 7 | component values | externals and straps from facts, each citing its fact | board script | pcb-datasheet-review slice 9 |
| 8 | schematic | parts + nets; each part line cites its selection record `# me<id> (why)` | board script → pcb | `precis-pcb-help`, `precis-workspace-help` |
| 9 | layout | topo place → topo route → post-route widening from `est_current_a` → check surface | pcb | `precis-pcb-topo-help`, `precis-pcb-check-help` |
| 10 | mechanical / environment | checklist `pcb-mech-env` answered | checklist on the pcb | this skill |
| 11 | review + fab | `op='review'`, then exports, with consigned lines separate | pcb | `precis-pcb-review-help`, `precis-pcb-fab-help` |

**Where the BOM lives** (td519926 asked this). There are three copies:

- **Source:** the board script's part lines.
- **Queryable:** the pcb design (`view='bom'`).
- **Published:** a draft table generated from `view='bom'`.

Only the source is edited. The published table is regenerated, never
hand-edited.

**Selection record shape.** A memory node, tags `decision`,
`selection`, `project:<slug>`, with this body template:

```
role: <what it does in the circuit>        e.g. energy storage, 12 h night
criteria: <ranked, each with its threshold> e.g. ESR < 1 ohm (no reservoir), leakage < 10 uA
candidates:
  - <MPN> (<C# or 'consign'>): chosen | rejected — <one-line verdict> [<fact handles>]
pick: <MPN> — <why, citing fact handles>
status: open | decided | provisional
```

The record carries these links:

- `derived-from` → each datasheet chunk, part fact or finding it rests
  on. There can be many; this is what the finding kind could not hold.
- `selects` → `part:<C#>` (or the component ref for a consigned part).
- `rejects` → each rejected part, with link meta `{reason}`.
- `serves` → the requirement node(s).
- An anchor link → `pcb:<slug>~#<refdes>` once the part is placed.

The script's part line cites the record. fi520367 is migrated to this
shape as the first worked example.

**Managing a record over the design's life** (Reto 2026-10-26: "manage
and update"). One record per role per design. It is edited in place
while it is `open`. Once it is `decided`, it changes only through these
moves, all with `edit(kind='memory')` plus links, and no record is ever
deleted:

- **Add or drop a candidate while open.** Edit the `candidates:` block
  and add or remove its `rejects` link. Each verdict line keeps its
  fact handles.
- **Re-select** (a better part, a failed review, the part going EOL,
  NRND or out of stock). Write a new record for the same role whose
  `pick` is the new part. Link `supersedes` → the old record, and set the
  old record to `status: superseded`. The new record copies the old
  rejects whose reasons still hold and turns the old pick into a
  `rejects` link whose reason says why it fell (for example "EOL per
  Digi-Key 2026-11-02"). The old record keeps its own links, so the
  history of why the design moved stays one walk. The board script's
  `# me<id>` cite is updated to the new record in the same change.
- **Keep rejects.** A rejected part is never unlinked when a record
  moves on, because the rejection reason is the reusable knowledge ("0.14
  F BestCap: two parts double the leakage"). A rejection proved wrong
  later is struck through in the body and gets `qualifies` → the fact
  that overturned it. It is not deleted.
- **Stale signal.** pcb-datasheet-review slice 8's lifecycle/stock
  staleness reads the record's `selects` target. A stale record shows on
  `#REFDES` as "selection stale: <reason>" until a re-select or an
  explicit "keep, reason" edit clears it.
- **Reuse across designs.** A later design's record may cite an earlier
  record's rejects with `derived-from` instead of re-arguing them. The
  `selects`/`rejects` rels are scoped to part selection (Reto: "yes, for
  part selection"). Architecture decisions (step 2) use `derived-from` and
  `serves` only.

**Budget convention.** The model is code in the workspace (the design's
script folder), with `assert` margins:

- `assert night_hours >= 12 * 1.25`.
- Every input constant carries a cite-the-DB comment, for example
  `I_SLEEP = 1.9e-6  # pc5354056 (nRF52833 System ON, RTC)`.

A run does three things:

- prints the tables;
- `put`s them as table chunks into the design draft, where the draft's
  graph figure re-renders from them;
- writes each rail's `est_current_a` onto the pcb nets.

That last write is the hand-off to post-route widening, so the budget and
the copper can't disagree. A failing assert blocks the publish.

## In scope

1. **Skill `precis-pcb-design-flow-help`** (a new router skill, each chunk
   ≤ 4000 chars). It holds:
   - the flow table above;
   - the selection-record template, its links and its life-cycle (open →
     decided → superseded; rejects kept);
   - the budget convention, with the energy model of a solar + supercap
     node as the worked example;
   - the BOM-home rule;
   - the "decisive fact" rule from step 5.

   `precis-pcb-help` "Canonical end-to-end" step 1 points to it.
2. **`precis-part-select-help` edits:**
   - the shortlist writes a selection record (template link);
   - the thin-stock ladder gains rung 6, "Consign", which points to (4).
3. **Relations `selects` / `rejects`** (decision → part/component; `rejects`
   carries `meta.reason`). Seeded in the relations registry
   (`store/types.py` + its seed migration) and listed in
   `precis-relations`. `#REFDES` on a pcb lists the decision records
   anchored to it ("why is this part here" in one call).
4. **Consigned BOM lines.** A pcb instance gets an optional `supply`:

   ```
   {mode: 'lcsc' | 'consign' | 'non-assembled',
    mpn, mfr, vendor?, lead_time_days?, as_of?}
   ```

   Default `lcsc` when a C-number is set. The field is reachable from the
   board script (`b.part(..., consign=Consign(mpn=..., ...))`) and from
   batch put. Its effects:
   - `bom_csv` keeps JLC's columns for assembled lines.
   - A second `consigned.csv` (MPN, manufacturer, qty, designators,
     vendor, lead time) goes in the fab zip.
   - `view='fab'` reports consigned lines as "consign: N lines, longest
     lead X d", not as "non-assemblable".
   - `view='availability'` skips them for stock and shows lead time.
5. **Checklist `pcb-mech-env`.** It applies when the board declares flex,
   a coating, an enclosure or an outdoor or wet environment. Items:
   - bend zones declared and free of vias and parts;
   - stiffener under connectors and heavy parts;
   - coverlay openings ≥ pad + 0.1 mm, with ≥ 0.15 mm to the next trace;
   - coating keep-off over sensors, connectors and test points;
   - a test or programming access path after coating;
   - mounting and strain relief;
   - operating temperature of every part ≥ the environment;
   - condensation and ingress.

   The items are judgment items worked by `pcb_review` like
   `pcb-tapeout`. Items whose geometry the pcb kind can't hold yet answer
   `not checkable (gr520329)` rather than pass.
6. **Budget helper**, minimal: `precis.workspace.budget.publish(draft=…,
   tables=…, nets={net: amps})`, which does the put + `est_current_a`
   write in one call. The model itself stays plain Python in the script.

## Explicitly NOT in scope

- Choosing the part ranking, lifecycle source or staleness: that is
  pcb-datasheet-review slice 8.
- Externals and straps from facts: pcb-datasheet-review slice 9.
- The part brief view (gr520243) and datasheet ingest for non-LCSC parts
  (gr519849).
- Flex geometry: fab layers, bend-zone features and stackup (gr520329,
  pcb-flexboard, pcb-stackup-regions).
- Router or placer behaviour of any kind (topo; the parallel topo design
  review owns it).
- Pours. Copper comes from post-route widening only.
- Link and thermal budget helpers beyond the energy example. The same
  convention applies; there is no extra code.
- Placing a consigned-parts order with JLC (`op='order'` keeps refusing
  consigned lines until Q3 is ruled).
- Architecture synthesis: the agent proposes the architecture, and the
  skill only says where it is recorded.

## Acceptance criteria

1. `get(kind='skill', id='precis-pcb-design-flow-help')` serves the
   11-step table, and each step names a kind and an owner skill.
   `search(kind='skill', q='where does the power budget live')` and
   `q='how do I record why I chose a part'` both rank it first.
2. Re-recording the TAZ supercap pick per the skill gives:
   - one memory selection record with 4 candidates, 1 `selects` and 3
     `rejects` links (each with a reason);
   - ≥ 3 `derived-from` links to datasheet chunks;
   - a `serves` link to the night-hold requirement.

   `pcb:taz-leaf-a~#C<supercap>` lists the record, and
   `search(kind='memory', q='which reflow supercap for a solar node and
   why')` finds it.
3. A board-script part with `consign=Consign(mpn='DMT334R2S474M3DTA0',
   lead_time_days=21)` gives:
   - no row in `bom.csv`;
   - one row in `consigned.csv`;
   - `view='fab'` "consign: 1 line, longest lead 21 d" and no
     non-assemblable warning for it.
4. `budget.publish` on the TAZ energy model writes two table chunks into
   the design draft. The draft's figure re-renders from them. `VBAT`'s
   `est_current_a` on the pcb equals the model's peak burst current. A
   failing margin assert publishes nothing.
5. `checklist pcb-mech-env` applies to taz-leaf-a (flex + parylene).
   `op='review'` gives a verdict or `not checkable (gr520329)` on every
   item, never a silent pass.
6. Re-selecting the TAZ supercap (mark CAP-XX EOL by hand) per the skill:
   - gives a new record that `supersedes` the old one;
   - the old record reads `status: superseded` and keeps all its links;
   - the new record carries the CAP-XX as a `rejects` with the EOL
     reason, plus the still-valid old rejects;
   - `#REFDES` shows only the new record as current, with the old one
     one hop back;
   - the board script's cite points at the new record.

## Target + blast radius

- New `src/precis/data/skills/precis-pcb-design-flow-help.md`. Edits to:
  - `precis-pcb-help` (end-to-end step 1);
  - `precis-part-select-help` (record + consign rung);
  - `precis-pcb-fab-help` (consigned.csv);
  - `precis-relations`.
- `src/precis/store/types.py` relations plus a seed migration (2 rels).
- pcb instance `supply` (meta, so no column unless a query needs one):
  - `store/_pcb_ops.py` apply;
  - `pcb/export.py` `bom_csv` + the consigned sheet;
  - the fab and availability views;
  - the workspace builder's `b.part`.
- Checklist seed for `pcb-mech-env` (same path as `pcb-tapeout`).
- `src/precis/workspace/` budget helper (or wherever pcb-workspace-code
  puts builder helpers).
- `#REFDES` view: the decision records anchored to the part.

## Slices (independently shippable, in order)

1. **Skill only** (in-scope 1, 2): docs, no code. It uses `related-to`
   with `meta.role = selects|rejects` until slice 2 lands. It unblocks the
   live TAZ dogfood at once, and fi520367 is migrated by hand as the
   worked example.
3. **Consigned lines** (4), with gr519924's BOM-home ask.
4. **Budget helper + `est_current_a` hand-off** (6). This waits on the
   topo realiser's widening (pcb-topo-place slice 5) only for its AC4
   copper check; the publish half ships alone.

## Open questions / decisions log

Rulings by Reto, 2026-10-26 (typed in the coordinator pane, relayed):

- **[decided] Q1 Record home: graph memory.** "yes in graph is good,
  manage and update." The selection record is a memory node, and its
  life-cycle is specified in §Managing a record over the design's life:
  edit while open, supersede on re-select, rejects kept.
- **[decided] Q2 Rel names: `selects` / `rejects`, scoped to part
  selection.** "seems good. yes, for part selection." Architecture
  decisions don't use them.

Open, none blocking (recommendations are DESIGN's):

- **Q3 Consign in fab.**
  - *Recommend:* a separate `consigned.csv` plus a fab-notes line.
    `op='order'` refuses while consigned lines exist until JLC's
    consigned-parts flow (parts shipped ahead and held in the JLC parts
    library) has been checked by hand once.
  - This needs one manual JLC check. It doesn't block slices 1–2.
- **Q4 Requirement nodes.**
  - *Recommend:* memory tagged `requirement`, linked `serves` from
    decisions, as pcb-datasheet-review ruled.
  - The project todo keeps its prose "Wanted" section and links the
    nodes. Not a blocker.
- **Q5 Where the budget script lives.**
  - *Recommend:* the design's workspace folder, next to the board script,
    per Reto's 2026-10-26 ruling that board scripts live in the DB, not
    git (td519926 comment 1).
  - Not a blocker.
