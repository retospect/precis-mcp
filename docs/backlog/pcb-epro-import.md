---
status: draft
title: "pcb: import an EasyEDA Pro .epro2 project into the pcb kind"
prio: high
model: opus
---

# EasyEDA Pro `.epro2` → `pcb`

## Motivation / why

Reto (2026-09-29) has a working-ish 4-layer board in EasyEDA Pro and wants to
iterate on it in precis: freeze the mechanicals (outline, screw holes,
connectors), annotate nets from datasheets, fix netlist bugs, re-route to the
new spec, iterate to fab. That entry point does not exist. `precis.pcb.easyeda`
parses exactly one thing — a single LCSC part's **Standard**-format footprint
document — and the `pcb` kind has no file intake at all: `Store.pcb_apply`
takes in-memory dict lists and nothing else. `pcb-guided-place-route.md` records
"EasyEDA Pro import" only as a named, never-specced post-ship follow-up.

## Spike result (2026-09-29) — the format is NOT the one KiCad documents

Reto exported a real 140-component 4-layer board (`heaterBaseTest.epro2`,
EasyEDA Pro editorVersion 3.2.149). It is **`.epro2`**, which shares the
record-type vocabulary of the `.epro` KiCad's dev-docs describe and almost
nothing else. The spike table S1-S12/R1-R3 in
[`pcb-epro-export`](./pcb-epro-export.md) was written against that older
format; the entries below **supersede** it, and the ones it no longer has a
question for are struck there.

| was assumed | is actually true |
|---|---|
| ZIP of `project.json` + `*.esch`/`*.epcb`/`*.efoo`/`*.esym` | ZIP of `project2.json` + `IMAGE/*.webp` + **exactly one `*.epru`**: every document concatenated into one stream |
| each line a typed JSON **array**, a dozen positional fields | each line `<header>\|\|<body>\|` — two JSON **objects**. Named keys throughout |
| several fields labelled `unk` (S5, the whole risk surface) | **no positional fields at all.** S5, S6, S7, S8, S9, S10 are moot |
| documents resolved by uuid through `project.json` maps (S4) | `type:"DOCHEAD"` opens a document; its `docType` is PCB/BOARD/SCH/SCH_PAGE/SYMBOL/DEVICE/FOOTPRINT/CONFIG/PANEL/BLOB. The manifest carries no maps (S2: 6 keys, all cosmetic bar `editorVersion`) |
| `REGION` with `flags` does outline + rule areas (S8) | there is no `REGION` type. The outline is a `POLY` on the layer whose `layerType` is `OUTLINE`; the spike board has exactly one |
| layer ids are a fixed table (3/4 silk, 11 edge, 15-44 inner) | `LAYER` records are **self-describing** (`layerId`, `layerType`, `layerName`, `use`). Derive, never hardcode. The spike board declares 92 and uses 27 |
| `VIA.unk2` might be the layer span (S6) | `viaType` + `unusedInnerLayers`. All 234 vias are `NORMAL` with nothing unused |
| a `HEAD`/`DOCTYPE` record might carry units/origin (S3) | no such record. `CANVAS` carries the user's grid and origin; units are mils regardless |

Confirmed unchanged: coordinates are **mils**, Y grows **down**.

**Three findings that would have cost real time:**

1. **The stream is a snapshot, not an event log.** `ticket`/`firstTicket` look
   like revision stamps and invite replay semantics. Across the board's 5149
   PCB records no `(type, id)` pair repeats. The reader asserts this rather
   than assuming it: a replay reader and a snapshot reader disagree exactly
   where it matters — which copy of a moved track is current.
2. **`POURED.pourFill` is in 10-mil units** while every other coordinate in
   the same document is in mils. Verified to four decimals on both axes
   against the outline. We discard `POURED` (it is derived fill), so it costs
   nothing today; in a reader that kept it, a silent factor of ten renders a
   plausible board.
3. **914 of the board's 1583 `PAD_NET` rows are stale**, pointing at
   components that no longer exist — 429 distinct component references for
   140 live components. The live 669 cover all 140 components and are exactly
   the rows with a non-empty body, so "has a body" is the liveness test. The
   net is `body.padNet`; the id tuple's fourth element *looks* like a net
   reference and is the footprint-local pad element id — it matched zero of
   the 244 net names. A reader keying on it produces an empty netlist and no
   error.

**R1 (rotation/mirror) is closed.** A pad's board position is
`component (x, y) + R(+angle) · (px, ±py)` in the Y-down frame; a bottom-side
part mirrors in **Y**. Settled by net agreement against routed copper: 573
track endpoints land on a same-net pad and **zero** on a pad of a different
net. All five other rotation/mirror candidates produce disagreements. Pinned
in `tests/test_pcb_epro_reader.py`, not just recorded.

**Still open:** R2 (`.epro2` vs `.eprj` for the write side) · whether inner
signal layers order by ascending `layerId` on a 6+-layer board (the spike
board's 1/15/16/2 does, but `LAYER_PHYS` carries a real `zIndex` stack order
with no link back to a `layerId`, so it cannot confirm) · `ARC` extraction
(the spike board routes with none) · S1 (does Pro open a project with no
schematic) and S12, both export-side.

## In scope

- ~~`src/precis/pcb/epro.py` — **pure** reader~~ **SHIPPED 2026-09-29**
  (slice 1a): container + stream split + `Frame` (mil→mm, Y-flip derived from
  the outline rather than assumed) + `layer_map`/`copper_layers` +
  `board_outline` + `extract_tracks`/`extract_vias`/`extract_copper`.
  Unknown record types are skipped, never fatal — `easyeda.py`'s rule.
  **Still to add there**: components/pads/pad-nets → `pcb_apply` shapes,
  footprints, mounting holes, `ARC`.
- `src/precis/ingest/pcb_epro.py` — the Store-facing half: intermediate →
  `pcb_apply` dict lists, stackup + plane application, optional fixed copper,
  idempotency, provenance stamping.
- `src/precis/cli/pcb.py` — `precis pcb import-epro PATH --slug S`, following
  `cli/add.py` (direct `Store` handle, parseable stdout, defined exit codes).
  CLI, not the MCP `put` surface: a real board is hundreds of pads.
- Mapping: `COMPONENT`+`ATTR` → `components[]` (`part_lcsc` stays NULL, or the
  design-local footprint is orphaned — `pcb_components.footprint` only joins
  `pcb_local_footprints` when `part_lcsc IS NULL`); `PAD_NET` → `connections[]`;
  `NET` → `nets[]` **name only**; the `POLY` on the `OUTLINE`-typed layer
  → the `outline` feature; non-plated `PAD`s → `mounting_hole` features;
  `FOOTPRINT` documents → `footprints[]`; `LAYER` → `pcb_boards.stackup`.
- **The one store change**: `_normalize_local_footprint` ignores any authored
  `pin_map` and always builds it identity, so a semantic pin name today requires
  naming the *pad* `"VDD"` — destroying the pad number the export half needs.
  Accept `f["pin_map"]` when given (validating each key is a real pad number),
  identity otherwise. Additive, no migration.
- Pin names come from the schematic half: the component's `Device` `ATTR`
  → the `DEVICE` document → its `SYMBOL` document's `PIN` records. The spike
  board carries 47 of each and 421 `PIN` records, so the chain is present;
  it has not been walked end to end yet. Degradation is printed, never
  silent.
- `--copper=none|fixed`, default `none`. `fixed` writes the rows
  `extract_copper` already produces via the already-public
  `store.pcb_fixed_copper_put(generator_name="epro:import")` (no migration
  needed), records `envelope = {"layers": n}` and nothing else, and ships a
  retire surface in the same slice. On the spike board that is **449 tracks
  and 234 vias** — 1579 source segments chained by `(net, layer, width)`, with
  a tee ending the polyline on every branch.

## Explicitly NOT in scope

- Pours as copper. `pcb_fixed_copper`'s CHECK is `track|via`; a single-net inner
  pour becomes a plane assignment, an arbitrary local pour is dropped with a
  warning, `POURED` is ignored entirely.
- Recovering a routing *sketch* from copper (trace-to-topology). Imported copper
  is frozen or discarded; there is no third option.
- Keepouts. The spike board carries 16 `RULE` + 168 `RULE_SELECTOR` records;
  precis has no keepout mechanism at all (`pcb_features.fixed` is inert), so
  anything imported from them would constrain nothing — the import summary
  must say so.
- Multi-board projects: v1 imports one board and requires `--board <uuid>`
  when the stream carries more than one `PCB` document. **Not theoretical**
  — the 2026-09-30 arc fixture is one project holding SEVEN `PCB` documents
  (PCB1..PCB7) and the interesting board is the LAST; `by_type("PCB")[0]`
  silently returned the wrong one. `EproProject.pcb()` now refuses and lists
  the candidates.
- EasyEDA **Standard** `.json` board documents. Different alphabet, separate
  item if ever wanted.

## Acceptance criteria

1. ~~The real board parses…~~ **MET 2026-09-29** for the copper half:
   `heaterBaseTest.epro2` parses to a 200.000 × 75.000 mm outline, 4 named
   copper layers, 449 tracks and 234 vias, with every coordinate inside the
   outline bbox and all 1579 source segments accounted for. The only warning
   is the 8 pours, which are out of scope by design.
2. After import: `view='svg'` shows the same board; instance count and
   connection count equal the source's `COMPONENT`/`PAD_NET` counts exactly;
   `view='drc'` runs; `op='route'` enqueues without refusing.
3. A named pin (`U1.SCL`) resolves to the right net, i.e. the `pin_map` join
   reaches real pads through `padplace.board_pads`.
4. **Import refuses** when: no outline `POLY` parses (an invented rectangular
   outline is a lie — every consumer otherwise falls back to
   `export.board_bbox`) — *done*; the stream is an event log rather than a
   snapshot — *done*; the archive is the older `.epro` — *done*; the slug is
   non-empty and `--update` was not given;
   the stackup is not 4 layers (`_enqueue_op` would refuse at first route
   anyway, and `_FIXED_COPPER_FAB_PROCESS` is pinned to `"4layer"`).
5. `--copper=fixed`: a realize run reports imported nets as already-realized
   rather than re-routing them; the retire surface removes every imported row
   and nothing else.
6. `--update`: re-import after moving two parts updates exactly those two
   poses, adds a new part, **reports** a removed one without retiring it, and
   does not duplicate the outline or the mounting holes.

## Target + blast radius

`src/precis/pcb/epro.py` (new) · `src/precis/ingest/pcb_epro.py` (new) ·
`src/precis/cli/pcb.py` · `src/precis/store/_pcb_ops.py`
(`_normalize_local_footprint`, `pcb_fixed_copper_put`/`_retire`,
`pcb_set_stackup`, `pcb_assign_plane`) · no handler view, no migration.

Fixture: `tests/fixtures/pcb_epro_tiny/` as **plain diffable text files**
(`project2.json`, `board.epru`), zipped in memory by a test helper — the
`easyeda_c*_trimmed.json` precedent. A committed binary `.epro2` would be
unreviewable and would rot silently. **Shipped**; still to add as the
netlist/placement slices land: a bottom-side component, a `DEVICE`/`SYMBOL`
pair for the pin-name chain, a non-identity pin map, an NPTH.

Real-board CI test: **no** — large and likely proprietary. Instead the
env-gated `PRECIS_EPRO_FIXTURE=/path/to/real.epro2` test (shipped; the
variable is on `scripts/test`'s passthrough allowlist), skipped when unset.

## Open questions / decisions log

- **Decided (Reto, 2026-09-29):** EasyEDA **Pro**, not Standard. 4-layer first;
  1/2/6/flex all wanted eventually. Copper: extract it as a starting point if
  that is not hard, otherwise just re-route.
- **Decided (Reto, 2026-09-30) — stale records are DROPPED on import.**
  "I suppose this is how they come. We make better ones on the round trip."
  A real export carries leftovers from earlier revisions: 984 of the spike
  board's 1583 `PAD_NET` rows are superseded (no payload; 914 of those also
  name a component the PCB no longer contains), and 154 of its 244 `NET`
  records reference no pad and no copper, plus one with an empty name.
  Importing them would put member-less nets into `pcb_nets`, where an
  existing net is reused by name on every later re-put — so they would
  survive forever in the unrouted census as something that can never be
  routed. `live_nets`/`live_pad_nets` implement the drop and report the
  counts; the import summary prints them rather than staying silent.
- **Decided (design, 2026-09-30):** the liveness test for a `PAD_NET` row is
  **has a payload**, checked before the component-existence test. On the
  spike board the payload test subsumes the component test (every orphan is
  also bodiless), and 70 further rows have a live component but no net — an
  unconnected pad, not a net named `""`. The component check is kept because
  the two are independent claims and it guards the expensive direction: a
  row WITH a payload naming a component that is gone.
- **Decided (design, 2026-09-29):** `--copper` defaults to `none`. Fixed copper
  is frozen forever, does not follow a part that moves, and `op='rip'` clears
  the sketch rather than fixed rows — copper attached to parts the user is about
  to move is a trap. One flag away when the board is mostly right.
- Duplicate pin names within one component (four `GND` pins) collide on
  `pcb_pins`' `(component_id, name)`. Proposed: rename to `f"{name}_{pad}"` in
  both `pins[]` and `pin_map`, record the rewrite in the warnings. Copper still
  lands correctly because `PAD_NET` binds per pad.
- ~~Rotation sign and bottom-side mirroring~~ **CLOSED 2026-09-29**,
  re-confirmed 2026-09-30 against ground truth Reto supplied (SW2 is top,
  U24 is bottom): both layer reads match, and scored on U24's pads alone
  mirror-in-Y gives 6 agreements / 0 disagreements while mirror-in-X gives
  exactly 0 / 6. `+angle`, bottom mirrors in Y.
- **`ARC` closed 2026-09-30.** `startX/startY`, `endX/endY`, `angle` (signed
  swept degrees), `width`, `arcType`. The centre is the perpendicular-
  bisector offset `(chord/2)/tan(sweep/2)` along the chord's left normal —
  exact on all five arcs of the arc fixture (|r_a-r_b| < 3e-13 mil,
  recovered sweep reproduces the stored one), including both signs of a
  semicircle. The Y flip is a reflection, so it REVERSES handedness: a
  positive stored sweep is `cw=True` in precis' frame. A zero or 360 sweep
  has no finite centre and is dropped, never chorded.
- **Open (Reto):** the spike board is 200 × 75 mm with 140 components and 8
  schematic pages. `op='place'`/`op='route'` refuse any stackup that is not
  exactly 4 layers, which this board is — but no route has been attempted at
  this size, so whether the router is usable on it at all is unmeasured.
