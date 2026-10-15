---
status: in-progress
title: "pcb: datasheet-grounded part selection, design and review — shared part facts, design-level derived facts, re-derivable verdicts, human review"
pillar: 3d-design
prio: high
model: opus
---

# pcb: datasheet-grounded part selection, design and review

## Motivation / why

Reto, 2026-10-13, dogfooding `pcb:taz15-blank` (imported from
`taz15.epro2`): once a netlist passes netlist DRC, an LLM should be able
to walk every part and pin against the datasheet and say "U3 needs a
bypass, it has one, done"; "U7 pin 23 must stay within 0–3.3 V and sits
on a 5 V net — alert, fix, re-check"; "pin 88 is limited to 1 A — is that
on the pin?"; "the datasheet gives address = 0x48 + 2·A1 + A0; A0 is tied
to GND and A1 to 5 V, so the address is 0x4A, but A1 must be ≤ VDD = 3.3 V";
"the datasheet wants X7R or better on VIN and the rating with margin —
C4 is Y5V 6.3 V on a 5 V rail". It must work the same on an imported
board and on an LLM-designed one, record what it has checked, and let a
human add review.

The designer side needs the same support (Reto, same session): for each
function, shortlist a few candidate parts, weigh availability and
lifecycle stage, read their datasheets, pick the best, then build the
circuit from that datasheet — the required externals, the strap values,
the margins. Selection, design and review read the **same part facts**;
designing from them and then checking against them is one mechanism used
twice, not two features. Today `precis-part-select-help` ranks by
Basic/stock/restock only, there is no lifecycle signal (NRND/EOL) and
no record of why a part won or what it beat.

What the dogfood found (2026-10-13, prod):

- **The import drops part identity.** `view='bom'` prints the refdes in
  the Comment column with no LCSC number for all 96 parts, so no datasheet
  pull was queued and `search(kind='datasheet')` finds nothing for the
  board. The source has it: `~/taz15.epro2` carries `Manufacturer Part`
  (594 attrs), `Supplier Part` (593, C-numbers), `Manufacturer`, `Pin
  Type` (372, IN/OUT/…), `NO_CONNECT` (11), values (`Resistance`,
  `Capacitance`) and datasheet `link`s. `epro.component_attrs` reads only
  `Designator`/`Footprint`/`Device`, and the instance reader leaves
  `part_lcsc` unset on purpose (setting it orphans the local footprint
  join — the docstring above the reader's `attrs = component_attrs(pcb)`).
- **ERC cannot run and cannot be fed.** `view='check', phase='netlist'`
  reports `pin_conflict`, `no_driver`, `over_voltage`, `rail_crossing`,
  `no_decoupling` all "CANNOT be checked" (no `etype`/`rail`/
  `max_voltage_v` on any pin). On an existing refdes a re-put skips
  `_pcb_insert_pins` (`Store.pcb_apply`, the `refdes in inst_by_refdes`
  branch; module docstring: "Re-put is create-or-extend, not a pin
  editor"), so nobody can add them after import.
- **Topology reasoning already works** from pin names alone: U9's `VCC`
  sits on `+3V3`, a two-member net (C17, U9) separate from the 30-member
  `3V3` rail; U7 `IO7`=`ADCBAT` and `IO15`=`ADC5VRAIL` have fanout 1;
  U16 pin 6 is alone on its net. Without part identity none of these can
  be argued to a verdict. (Real bug or importer net-port loss — slice 1
  decides.)
- **The ledger exists.** `pcb-tapeout` applies by default (45 items, 42
  not checked); verdicts are anchored, go stale on target change, carry
  question/answer threads. What it lacks is staleness on *knowledge*
  changes and a split between disposable machine verdicts and sticky
  human ones.

## The model — three layers, three lifetimes

| layer | lives on | example | lifetime |
|---|---|---|---|
| **Part facts** | the part, keyed by manufacturer + manufacturer part number, found by that, by LCSC C-number, or by full parametric description (size, value, class) for generic passives; shared by every design | "pin 5 A1: digital input, VDD domain, abs-max VDD+0.3 V"; "address = 0x48 + 2·A1 + A0"; "CIN ≥ 10 µF X5R or better" | durable; each fact page-cited; trusted by default, an unclear or contradictory fact raises an issue; a human can confirm or correct |
| **Design intent** | the graph memory: requirement and decision nodes linked to each other and anchored to the design's refdes/nets | "make the status light blink" ← served by "LED + driver + 3V3 supply"; "U16 = TPS62133 for 3V3 @ 1.5 A: chose it over MP2359 (NRND) and AP62300 (thin stock)"; "A1 strapped high on purpose" | durable; authored (human or agent), never re-derived |
| **Derived facts + verdicts** | the design, keyed by their inputs | "U5 I²C address = 0x4A (A0→GND, A1→5 V)"; "C4 rating 6.3 V < 2 × 5 V — fail" | disposable: dropped and recomputed when any input changes — the netlist, a part fact, the rule/procedure version |

The third layer is what "forget and re-derive" means: a verdict records
the inputs it consumed (anchors' fingerprint, part-fact revisions, rule
or skill revision). Learning something about the process (a better rule,
a corrected extraction, a revised skill) bumps a revision, and every
verdict that consumed it reads `stale` and is recomputed on the next
pass. **Human verdicts are not forgotten**: a stale human verdict stays
visible as stale and asks a human to re-confirm; it is never silently
replaced by a machine one.

**Design intent is a mesh, not a field.** A requirement ("the light
blinks") is served by situational choices (which LED, which driver,
which supply) that depend on the device, its environment and the field's
general practice; the reasons are linked to other reasons. Store them as
graph-memory nodes: requirement → `served-by` decision nodes (the
existing acyclic `serves` relation, already used by the quest roadmap),
decisions `derived-from` the part facts and datasheet chunks they rest
on, `qualifies` for caveats. Anchor each node to refdes/net handles so
`#U16` lists the decisions about it and a decision lists what it touches.
Discoverability is the point: "why is this part here" is one walk.
Child order is derived at read time (requirement severity, then
recency); a stored sibling order is YAGNI until a reader needs it.

## In scope

1. **Import completeness (epro).** Read the `DEVICE` attributes into the
   component: `Manufacturer`, `Manufacturer Part`, `Supplier`/`Supplier
   Part` (LCSC C-number), value (`Resistance`/`Capacitance`/… →
   `label`), datasheet `link`, symbol `Pin Type` → `etype` (enough for
   ERC; a datasheet fact refines it and wins on conflict), `NO_CONNECT`
   → `nc`. Carry the C-number *without* breaking the local footprint join
   (a separate column or join rule; the current `part_lcsc IS NULL`
   condition is the thing to change, not to work around). The import
   queues datasheet pulls like any put. Verify the schematic net-port
   joins (power ports, net labels) against the source: the `+3V3`/`3V3`
   split must be either reproduced from the source or fixed in the
   importer. Locked source components already map to `fixed: 'both'`
   (`epro.py`, `if b.get("locked")`); the TOC has no column for it yet —
   add one. Part lookup on import: manufacturer + MPN first, then the
   C-number, then the parametric description for generic passives.
2. **Pin annotation on existing parts.** `put(args={'op':'pins',
   'refdes':…, 'pins':[{name, etype?, rail?, max_voltage_v?, vih?, vil?,
   max_current_a?, nc?, unused?, must_terminate?, source?}]})` patches
   pin semantics on an existing instance (design layer). `max_current_a`
   is new and feeds an `over_current` rule against net `current`. Pad
   bindings stay out (the canonical-pad rule stands).
3. **Part pin facts** (shared): extends `datasheet-facts-mesh.md` — its
   pin table gains the electrical columns above, plus two fact shapes it
   does not have: **required externals** (`{pin(s), part class, value
   range, dielectric ≥, voltage ≥, distance ≤, page}`) and **conditional
   tables** (strap/address/mode: `{input pins, levels → output}`, page).
   A design's instances inherit part facts as defaults; the design layer
   (2) overrides per board with a reason. ERC reads the merged view.
4. **Derived facts.** Computed per instance from part facts + this
   design's connections, stored with their derivation: I²C address from
   the address table and the strap nets' rails; a pin's effective domain
   from its rail; a passive's rating and dielectric parsed from the part
   catalog description (`C1525` → `100nF 16V X7R`). Shown on `#REFDES`.
5. **Deterministic rules** on the check surface: the existing ERC set,
   now fed; plus `required_external` (missing or wrong-class external part),
   `passive_rating` (voltage/power rating against net voltage × margin;
   dielectric rank C0G > X7R > X5R > Y5V/Z5U against the part fact's
   minimum), `bus_address_conflict` (two derived addresses equal on one
   bus), `over_current`. Margins are a short named policy (cap ≥ 2 × working V for
   X5R/X7R, ≥ 1.5 × for C0G; resistor ≤ 50 % rated power), not a table —
   and the policy has a revision that verdicts consume.
6. **Re-derivable verdicts.** Checklist verdicts and derived facts record
   their inputs (anchor fingerprint, part-fact revisions, rule/policy
   revision, skill revision); staleness extends from "target changed /
   item revised" to "a consumed input changed". `op='forget'` drops
   machine-origin verdicts for a target (optionally per item or per
   rule revision) so the next pass recomputes them; human verdicts are
   kept.
7. **Re-check pass.** One job, `pcb_review`, parented to the design:
   recompute derived facts, run the check surface, then work only `not
   checked` and `stale` judgment items per the skill (8), writing anchored
   verdicts with the decisive datasheet line quoted. A re-run on an
   unchanged design makes no LLM calls. Missing part facts read `not
   checkable (no datasheet facts for <part>)`, never pass.
8. **Skill `precis-pcb-review-help`.** The walk: netlist check clean →
   per IC: open part facts (or extract them, cited) → per pin: domain,
   levels, current, strap/address → required externals present and
   adequate → record the verdict anchored to refdes/nets. The margin
   policy lives here in prose with the same revision as (5). Fold the I²C
   address arithmetic into `precis-i2c-help`.
9. **Selection — the shortlist.** For a function with requirements
   (`{function, rail in/out, current, interface, package limits}`):
   2–4 candidates from `search(kind='part')`, datasheets pulled, part
   facts extracted (3), each scored on fit (facts vs requirements, with
   margin), availability (Basic, stock relative to need, restock signal),
   **lifecycle** (active / NRND / EOL / unknown — a new part attribute
   from a supplier source; `unknown` is shown, never read as active) and
   cost. The pick is recorded as design intent with the requirement, the
   winner's decisive facts and each rejected candidate's reason, anchored
   to the refdes. A lifecycle or stock change on the winner later marks
   the selection stale (the `bom-availability-lifecycle` checklist item
   reads it).
10. **Design from facts.** Placing a selected part instantiates its
   required externals from the part facts (CIN/COUT/bootstrap/pull-ups,
   with value, dielectric and rating chosen to meet the margin policy,
   themselves selected via 9 for the passives' availability) and sets
   straps from the conditional tables to hit the wanted mode/address.
   Every generated part notes the fact it came from. The review pass (7)
   then checks the result; deterministic rules (5) are the independent
   check, since the same model wrote the design.
11. **Skill update.** `precis-part-select-help` gains the shortlist
   procedure, lifecycle, and the recorded decision; a short
   design-from-facts section (or a `precis-pcb-design-help`) covers 10.
12. **Provenance and adversarial re-check** (folded from
   `pcb-design-source-provenance.md`). Every part fact and design-intent
   node cites its source chunk (datasheet, app note, reference design —
   app notes and reference designs ingest as `datasheet` with a
   `subtype`; nothing new to build there). The stored citation is a
   claim: the fresh-context reviewer re-reads the cited chunk cold and
   tries to falsify it — pass quotes the decisive line, a missing or
   dangling citation is `not checkable`, a contradicting chunk is a fail
   with the discrepancy in the item's thread. Reverse query: "which facts
   and decisions came from document X", so a vendor revision re-opens
   exactly those (re-ingest staleness rides the same input-revision
   mechanism as 6).
13. **Human review.** The board page (`pcb-argue-with-design.md`) gets a
   review state per verdict and per part fact: `confirm` / `reject` with
   one line of reason, written as a human-origin verdict (or a
   `confirmed` mark on the part fact). A queue view lists machine
   verdicts and extracted part facts no human has confirmed, highest
   severity first.

## Explicitly NOT in scope

- The datasheet fetch/ingest (shipped, `datasheet_pull`) and the
  extraction mechanism itself (`datasheet-facts-mesh.md` owns it; this
  item adds fact shapes and a consumer).
- DC-bias derating curves as stored data: a close call fetches the
  manufacturer curve and quotes the number in the verdict evidence.
- Simulation (SPICE) of any kind.
- Auto-fixing: the review proposes edits (as `pcb_argue` does); it does
  not apply them.
- Pin swap / capability lattice (`pcb-component-model.md`).
- Whole-board architecture synthesis (which blocks a product needs): the
  designer starts from named functions with requirements.
- Supplier parametric import beyond lifecycle and stock
  (`digikey-component-import.md` owns supplier ingest).

## Acceptance criteria

1. Re-importing `taz15.epro2` gives a BOM whose Comment column carries the
   value and whose LCSC column is filled for every part the source has a
   `Supplier Part` for; datasheet pulls are queued for them; pads still
   render (the local footprint join holds). The `+3V3` net either matches
   the source or the importer is fixed and it merges.
2. After part facts exist for U7 and U9, `view='check', phase='netlist'`
   on taz15 has no "CANNOT be checked" row for `over_voltage`,
   `rail_crossing`, `no_decoupling`; U9 on `+3V3` produces a finding.
3. For each I²C device on `SDA`/`SCL`, `#REFDES` shows a derived address
   with its derivation (`A0→GND, A1→3V3, table p.12`), and two equal
   addresses raise `bus_address_conflict`.
4. A capacitor with rating < margin × net voltage, or a dielectric below
   the part fact's minimum, raises `passive_rating` naming both numbers.
5. `pcb_review` run twice on an unchanged design: the second run makes zero
   LLM calls and changes no verdict. Bumping the margin-policy revision
   marks exactly the verdicts that consumed it `stale`; human verdicts on
   the same items stay, shown stale, not replaced.
6. A part fact corrected on part `C…` marks stale the verdicts on every
   design that consumed it (shown by the `contains` edge from part to
   boards).
7. A shortlist run for "3V3 buck, 12 V in, 1.5 A" records 2–4 candidates
   with fit, availability, lifecycle and cost each, the winner and every
   rejection reason; marking the winner NRND marks the selection stale.
8. Placing the winning buck adds its required externals from the part
   facts with cited values, and a `pcb_review` pass on the result passes
   `required_external` and `passive_rating` with no human edits.
9. The board page shows the review queue; confirming a verdict records a
   human-origin verdict that survives `op='forget'`.

## Target + blast radius

- `src/precis/pcb/epro.py` (`component_attrs`, the component reader),
  `src/precis/ingest/pcb_epro.py`, the `part_lcsc`/local-footprint join in
  `store/_pcb_ops.py`
- `store/_pcb_ops.py` pin upsert (`op='pins'`), `handlers/pcb.py` op
  dispatch, `src/precis/pcb/drc.py` ERC rules
- part ref facts (`datasheet-facts-mesh.md`), `measures`
- checklist store (verdict inputs, `op='forget'`, origin), the
  `pcb-tapeout` checklist items, a new `pcb_review` job type
- `src/precis/pcb/argue.py` + board page (review state, queue)
- graph memory (design-intent nodes, `serves`/`derived-from` links
  anchored to pcb handles); `checklist-kind.md`'s per-item TTL lands here
  as lifecycle/stock staleness
- skills: new `precis-pcb-review-help`; `precis-pcb-check-help`,
  `precis-checklist-help`, `precis-datasheet-help`, `precis-i2c-help`,
  `precis-pcb-help` (op='pins', import fields)

## Slices (independently shippable, in order)

1. ~~Import completeness (in-scope 1)~~ — **SHIPPED 2026-10-14** (branch
   `worktree-p1-epro`, tip `9a786251d`, on the pcb-easyeda-round-trip
   thread). What it landed: `Supplier Part`/`LCSC` → `part_lcsc`,
   value/`Resistance`/`Capacitance`/… → `label`, `Manufacturer Part`/
   `Manufacturer`/`Supplier`/`link`/`JLCPCB Part Class` → `meta.part`;
   symbol `Pin Type` → pin `etype` (Undefined → unset), schematic
   `NO_CONNECT` → pin `nc`; the local footprint join is local-FIRST so
   the C-number can no longer orphan pads (identical local footprints are
   dropped at import, the verdict recorded per instance); datasheet pulls
   fire for imported C-numbers and `link_known` attaches already-held
   sheets one link each; part lookup is mfr+MPN → claimed C-number →
   unique parametric (package+value); the TOC gained a `fixed` column.
   Verified: the `+3V3`/`3V3` split IS the source's topology (a real
   two-member `+3V3` net: C17.2 + U9.VCC — not an importer bug). The
   AC's "re-importing taz15" proof is `test_taz15_import_export_
   reimport_structural` (85 C-numbers, all attrs + pin semantics
   verbatim). NOT shipped: pin `max_voltage_v`/`vih`/`vil`/`rail`
   (belongs to slices 2+/facts), `Device`/`Symbol` uuid retention,
   schematic wire/netlist import proper (the +3V3 check confirmed the
   *board's* pads, not sch-sheet fidelity).
2. `op='pins'` + `max_current_a` + `over_current` (2) — makes the existing
   ERC usable on imported boards by hand. **Shipped 2026-10-14**
   (worktree `ds-review`): `put(args={'op':'pins','refdes':…,'pins':[…],
   'source':…})` patches `etype`/`rail`/`max_voltage_v`/`vih`/`vil`/
   `max_current_a`/`current_role`/markers onto an existing instance's
   pins, validated against the component's real pin names, idempotent,
   `source` recorded in pin meta; `check_over_current` joins the ERC set
   (`shunt` is the one `current_role` exemption); `over_voltage`/
   `over_current` report per-net `… unknown for net X: ask the user`
   info rows rather than guessing an unannotated net.
3. ~~Passive rating/dielectric from catalog description + margin policy
   (4 partially, 5 `passive_rating`)~~ — **SHIPPED 2026-10-14**
   (worktree `ds-review`, on `a7863eda3`'s slice-1 catalog join):
   `catalog.parse_passive_description` parses a `parts.description` into
   `{kind, value, rated, dielectric}` (never guesses — a field that does
   not pattern-match stays `None`); `pcb_graph` hoists
   `part_description` onto instances and `#REFDES` shows the derived
   fact; `op='pins'` gains `min_dielectric` — the design-layer stand-in
   for the part fact's `dielectric ≥`, declared on the consumer's pin;
   `drc.check_passive_rating` joins the ERC set — cap rated ≥ factor ×
   the voltage across it (named policy `fixed-derating` r1: 2×, 1.5× for
   C0G/NP0), resistor dissipation ≤ 50 % of rated (V²/R, or I²R through
   a `series` pin when only current is known), dielectric rank ≥ the
   declared minimum — with per-part `… unknown … ask the user` and
   cannot-parse not-checked rows, never a silent pass.
4. ~~Verdict inputs, origin, `op='forget'`, staleness on consumed
   inputs (6)~~ — **SHIPPED 2026-10-14** (worktree `ds-review`):
   migration 0213 adds `checklist_verdicts.origin` ('human'|'machine';
   existing rows backfill 'human' honestly) and `.inputs`
   (`{name: revision}` consumed-input map — `margin:fixed-derating`,
   later fact/rule/skill revisions). `edit(op='verdict')` takes
   `origin=`/`inputs=`; `compute_statuses` marks a verdict
   `stale (input changed: X)` when the kind bridge's `input_revision`
   disagrees — human verdicts included (shown stale, never replaced);
   an unresolvable name never marks stale. `PcbBridge` resolves
   `margin:fixed-derating` → `PASSIVE_MARGIN_REVISION`. `edit(op=
   'forget', target=…, item=?, consumed=?)` retires live machine-origin
   verdicts only. The "consumes the whole part" fallback lands with
   slice 5's fact ids.
5. Part fact shapes (3) — with/after `datasheet-facts-mesh.md`'s pin table.
6. Derived facts + `required_external`, `bus_address_conflict` (4, 5).
7. `pcb_review` job + `precis-pcb-review-help` (7, 8).
8. Lifecycle attribute + the shortlist and recorded selection (9, 11).
9. Design from facts — required externals and straps (10).
10. Provenance reverse query + adversarial re-check (12).
11. Human review state + queue on the board page (13).

## Open questions / decisions log

Rulings by Reto, 2026-10-13 (this session), answering the first draft's
questions:

- **[decided]** Trust: part facts are trusted by default; raise an issue
  where the datasheet is unclear or contradicts itself. No human
  confirmation gate.
- **[decided]** Independence: judgment items are reviewed in a fresh
  context (not the context that designed or extracted). A different
  model tier is not required to start.
- **[decided]** Design intent lives in graph memory, linked to other
  reasons (see "Design intent is a mesh"). Requirement trees use `serves`;
  stored sibling order is YAGNI.
- **[decided]** Net voltages on imported boards: ask the user when a rail's
  voltage is not derivable; do not guess.
- **[decided]** Re-import: re-import part identity onto a fresh slug and
  route it with our router and correct copper specs (net current and
  voltage from the facts) rather than preserving the source routing.
- **[decided]** Part identity: key by manufacturer + MPN; find by MPN, by
  LCSC C-number, and by full parametric description (size, value,
  dielectric/class, rating) for generic passives.
- **[decided]** Margins: from the manufacturer's published derating
  guidance per manufacturer/family where it exists (a manufacturer
  table); the fixed policy (2× for X5R/X7R, 1.5× for C0G, 50 % resistor
  power) is the fallback. Both carry a revision verdicts consume.
- **[decided]** EasyEDA symbol pin types are enough for ERC; datasheet
  facts refine and override them.
- **[decided]** Lifecycle source: Digi-Key (`digikey-component-import.md`)
  or Octopart/Nexar, by MPN, cached on the part with `as_of`, re-checked
  before each order.
- **[decided]** Extraction budget: one BIG-tier read per IC datasheet;
  passives use the catalog description.
- **[decided]** Storage: no new store. Part facts, design intent and
  derived facts use the existing graph (refs, links, memory) and the
  numeric extraction as it is being built (`measures-substrate.md`,
  `datasheet-facts-mesh.md`, `corpus-quantitative-extraction.md`); this
  item adds fact shapes and consumers on top, and follows those items'
  schema as they land.
- **[decided]** Staleness granularity: per fact, per-part fallback. A
  verdict records the fact ids it read; deterministic rules know theirs
  exactly, and a judgment verdict cites the facts it used as evidence. A
  correction re-opens only verdicts that read the corrected fact. A
  verdict that cites no facts consumes the whole part.
- **[decided]** Conditional tables: stored as printed (rows of input-pin
  levels → output value, page anchor), derived by one generic lookup
  that reads each input pin's net, classifies it (GND/VDD/SDA/SCL/
  resistor) and finds the row. Resistor-programmed addresses become a
  table of resistor ranges. No stored expressions unless a part has no
  printed table.
- **[decided]** `current_role: series | shunt | signal` comes into
  `op='pins'` here (slice 2), since `over_current` needs it;
  `pcb-component-model.md` keeps the defaulting rule that also reads it.
- **[decided]** Interpretations confirmed: re-import to a fresh slug and
  route with our router (not preserving source routing); margins from
  the manufacturer's derating guidance with the fixed policy as fallback.

No open questions remain; the item can go to `/ready`.

Ready-gate findings, 2026-10-13:

- **blocker** — Bundles ~4 independently shippable deliverables (13 scope items, 11 slices, 2 deps still draft); split: A = slices 1-3 (import, op='pins', passive_rating; no deps), B = slice 4 (verdict inputs/origin/forget; checklist substrate), C = slices 5-7,10-11 (needs datasheet-facts-mesh, blocked-by it), D = slices 8-9 (selection/design; needs C + digikey-component-import for lifecycle). A whole-item `blocked-by` would wrongly gate A.
- **blocker** — Where derived facts and verdict inputs persist is unnamed under "no new store" (`_VERDICT_COLS` in `store/_checklist_ops.py` has one `fingerprint` and `checked_by`, no `origin`, no list of consumed fact ids/revisions); name the columns/link kind or defer to the facts-mesh schema.
- **advisory** — AC6 says the `contains` edge runs "from part to boards"; in `_pcb_ops.py` the edge is board(src) -> part(dst); reverse query only.
- **advisory** — Slices 4-5 acceptance rows (op='forget', provenance reverse query slice 10) have no AC beyond 5/9; add ones for 12 and for lifecycle ingestion (AC7 only covers a manual NRND mark).
- **advisory** — `checklist-kind.md` (draft) hands per-item TTL to this item; keep both in sync when splitting.
