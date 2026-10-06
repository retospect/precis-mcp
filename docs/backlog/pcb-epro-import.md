---
status: draft
title: "pcb: import an EasyEDA Pro .epro2 project into the pcb kind"
prio: high
model: opus
pillar: 3d-design
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
- ~~components/pads/pad-nets → `pcb_apply` shapes, footprints, mounting
  holes~~ **SHIPPED 2026-09-30** (slice 1b): `component_attrs`,
  `symbol_pin_names`, `device_pin_names`, `footprint_title`, `pad_numbers`,
  `extract_footprints`, `extract_components`, `extract_mounting_holes` and
  the one entry point `build_design` → a `Design` dataclass.
- ~~`src/precis/ingest/pcb_epro.py`~~ **SHIPPED 2026-09-30**: `derive_stackup`
  + `import_epro` (ordering, refusals, plane application, provenance
  stamping, `dry_run`).
- ~~`src/precis/cli/pcb.py` — `precis pcb import-epro PATH --slug S`~~
  **SHIPPED 2026-09-30**, with `--dry-run`, `--board UUID`, `--title`, and
  two distinct failure codes (`EXIT_UNREADABLE=2` the file is not readable,
  `EXIT_REFUSED=3` the file is fine and precis will not take it as it
  stands) — a caller needs to tell "re-export this" from "wait for a slice".
- ~~Mapping~~ **SHIPPED**: `COMPONENT`+`ATTR` → `components[]` (`part_lcsc`
  stays NULL, or the design-local footprint is orphaned —
  `pcb_components.footprint` only joins `pcb_local_footprints` when
  `part_lcsc IS NULL`); `PAD_NET` → `connections[]`; `NET` → `nets[]`
  **name only**; the `POLY` on the `OUTLINE`-typed layer → the `outline`
  feature; non-plated `PAD`s → `mounting_hole` features; `FOOTPRINT`
  documents → `footprints[]`; `LAYER` → `pcb_boards.stackup`.
- ~~**The one store change**: `_normalize_local_footprint` ignores any
  authored `pin_map`~~ **SHIPPED 2026-09-30**. It now honours an authored
  `pin_map` (bare `'PIN'` string or the full `{name, tags}` row), identity
  for any pad it omits, and refuses a key that is not a real pad number —
  that failure is otherwise silent, since a stale key simply matches no pad
  and the pin it meant to name falls back to the number with nothing said.
- ~~Pin names come from the schematic half~~ **CLOSED 2026-09-30 — the
  chain is walked end to end.** `COMPONENT` → `ATTR[Device]` → the `DEVICE`
  document's `META.attributes["Symbol"]` → the `SYMBOL` document's `ATTR`
  records keyed `Pin Number`/`Pin Name`, **paired by `parentId`**. The
  `PIN` records themselves carry geometry only — no name and no number — so
  a reader that looked at `PIN` would find nothing. Measured on the spike
  board: 140/140 components resolve a Device, a Symbol and a non-empty pin
  map; 40 of those maps are non-identity (real signal names), and no symbol
  names a pad its footprint lacks.
- ~~`--copper=none|fixed`, default `none`~~ **SUPERSEDED (Reto,
  2026-09-30): do not build fixed-copper import at all.** "We extract the
  points and fix our model to match, then regenerate." Imported copper is
  a **measurement**, not geometry to keep: the value is comparing the
  source board's own trace widths, clearances and via sizes against what
  precis' rules would produce, so the SPEC gets corrected and the board is
  re-routed from it. Freezing the copper would preserve the very decisions
  the round trip exists to revisit, and it does not follow a part that
  moves.
  So slice 1c is a **report**, not a write: no
  `store.pcb_fixed_copper_put` call, no `envelope`, and no retire surface
  (nothing is written, so there is nothing to retire — the one-slice
  coupling that rule existed for is moot). **SHIPPED 2026-10-01 (report
  half):** `Extraction.rows` and `extract_copper` are deleted;
  `epro.measured_copper` feeds `precis.pcb.copper_report`, which every
  import prints and `precis pcb copper-report PATH --slug S` re-runs
  against the slug's CURRENT net rules (so an annotation's effect is
  visible). Pad-to-pad gaps are excluded (a footprint fact, not a routing
  decision); gaps are probed to 1.0 mm.
  On the spike board the measurement covers **449 tracks and 234 vias**
  (1579 source segments chained by `(net, layer, width)`, a tee ending the
  polyline on every branch).

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
2. ~~After import: `view='svg'` shows the same board; instance count and
   connection count equal the source's `COMPONENT`/`PAD_NET` counts exactly;
   `view='drc'` runs; `op='route'` enqueues without refusing.~~ **MET
   2026-09-30**, and re-checked against the real board at full scale
   (`PRECIS_EPRO_FIXTURE`): 140 components, 20 footprints and 599
   connections in one `pcb_apply`, every pad resolving through its imported
   pin map to a position inside the outline, no duplicate connection.
3. ~~A named pin (`U1.SCL`) resolves to the right net, i.e. the `pin_map`
   join reaches real pads through `padplace.board_pads`.~~ **MET
   2026-09-30** — on a synthetic footprint
   (`tests/test_pcb_footprint_pin_map.py`) and on the imported one.
4. **Import refuses** when: no outline `POLY` parses (an invented rectangular
   outline is a lie — every consumer otherwise falls back to
   `export.board_bbox`) — *done*; the stream is an event log rather than a
   snapshot — *done*; the archive is the older `.epro` — *done*; the slug is
   non-empty and `--update` was not given — *done* (and `--update` exists);
   the stackup is not 4 layers (`_enqueue_op` would refuse at first route
   anyway, and `_FIXED_COPPER_FAB_PROCESS` is pinned to `"4layer"`) —
   *done*. A `--dry-run` reproduces every refusal, so none is a surprise on
   the real import.
5. **MET 2026-10-01.** On the real board: 57 of 89 nets disagree — 9
   wider than the author's 0.254 mm default (current-annotation
   candidates), 50 with 0.102 mm gaps against precis' 0.150 mm rule; none
   below the fab minimum. It also exposed an import bug, now fixed: inner
   pour layers the author routed on were stacked as pure planes
   (`derive_stackup` now marks them `routable`). **The copper REPORT** (slice 1c, reshaped by Reto's 2026-09-30 ruling):
   for the imported board, print the source's own measured minima — track
   width, clearance, via diameter and drill, per layer — beside what
   precis' own rules would resolve for the same nets, so a disagreement is
   visible and the spec can be corrected before re-routing. Never writes
   copper. A net whose source width is BELOW the process floor is the
   interesting case and must be called out, not averaged away.
6. **MET 2026-10-01** (`ingest/pcb_epro.py::plan_update`). `--update`:
   re-import after moving two parts updates exactly those two poses, adds
   a new part, **reports** a removed one without retiring it, and does not
   duplicate the outline or the mounting holes. Decided while building it:
   only poses (incl. a side flip) and new parts are APPLIED; a rewired pin
   on a surviving part, a removed part and a changed outline/hole set are
   reported, because precis is where the design is being corrected and a
   re-import that applied them would silently undo a fix made here. A
   locked part IS moved (the lock guards against the optimizer, not the
   author) and the report says so. Refused: no such slug, a slug not
   imported from EasyEDA, a different board UUID, a surviving part whose
   footprint changed. The stackup is not re-derived — a change is warned.

## Target + blast radius

`src/precis/pcb/epro.py` (new) · `src/precis/ingest/pcb_epro.py` (new) ·
`src/precis/cli/pcb.py` · `src/precis/store/_pcb_ops.py`
(`_normalize_local_footprint`, `pcb_fixed_copper_put`/`_retire`,
`pcb_set_stackup`, `pcb_assign_plane`) · no handler view, no migration.

Fixture: `tests/fixtures/pcb_epro_tiny/` as **plain diffable text files**
(`project2.json`, `board.epru`), zipped in memory by a test helper — the
`easyeda_c*_trimmed.json` precedent. A committed binary `.epro2` would be
unreviewable and would rot silently. **Shipped**, and extended 2026-09-30
with everything slice 1b needed: a bottom-side rotated component on an
asymmetric footprint (the handedness case), a `DEVICE`/`SYMBOL` pair per
component for the pin-name chain, a non-identity pin map including a
duplicate `GND`, a THT pad, an obround pad, an NPTH free pad and a plated
free pad. 7 documents, 73 lines, all diffable text.

Real-board CI test: **no** — large and likely proprietary. Instead the
env-gated `PRECIS_EPRO_FIXTURE=/path/to/real.epro2` test (shipped; the
variable is on `scripts/test`'s passthrough allowlist), skipped when unset.

## Slice 1b spike results (2026-09-30)

Measured on `heaterBaseTest.epro2`; each one changed the code.

| finding | why it bites |
|---|---|
| **A bottom-side instance imports as `rot = (angle + 180) % 360`.** precis mirrors a bottom instance's pads in **X** (`padplace._transform_local_point`); EasyEDA mirrors them in **Y**. Those two reflections differ by exactly a half turn. Verified against `padplace.place_pad_point` over all 84 side × rotation × pad combinations, not by hand algebra | without it every bottom-side pad lands diametrically opposite its true position — renders correctly in every view, unbuildable. 54 of the board's 140 parts are on the bottom |
| **Several PADs may share one `num`** (`SMD-1_BD8.7-D6.2`: three pads numbered `1`) and they are ONE pin | one row per PAD gives the component duplicate pins and duplicate connections for the same copper; and the "rename duplicates apart" rule collides with itself, since the suffix is that same shared number |
| Pin names live on the SYMBOL's `ATTR` records (`Pin Number`/`Pin Name`, paired by `parentId`), **not** on `PIN` | `PIN` carries geometry only; a reader keying on it finds no names at all |
| `defaultPad.padType` ∈ `RECT` (256) · `ELLIPSE` (103) · `OVAL` (40) · `POLYGON` (4). Every `ELLIPSE` is circular (w == h) and every `RECT` radius is 0 **on this board** | both are expressible and neither has a precis shape — a non-circular ellipse degrades to `obround` and a rounded rect loses its radius, each with a warning rather than silently |
| `hole` is `null` on SMD pads and `{holeType, width, height}` on THT (111 of 403). Two pads carry `holeType: "SLOT"` 81.89 × 39.37 mil | precis carries one drill DIAMETER, so a slot imports as a round hole of its long axis — warned per pad, because it changes what the fab drills |
| The PCB document carries 30 **free** PADs belonging to no component: 24 non-plated 6.0 mm (the mounting pattern) and 6 plated 10.0 mm on `GND` | only the non-plated ones are mounting holes. A plated free pad is copper on a real net with no precis model at all — turning it into a hole would drop its net, so it is reported and skipped |
| `FOOTPRINT` documents carry a `META.title` (`SW-SMD_4P-L5.1-W5.1-P3.70-LS6.5-TL_H1.5`); 33 distinct titles for 33 documents, and the project places 20 of them | the uuid is what a COMPONENT names but is meaningless in `view='bom'`. Only placed footprints are imported; a title collision and a footprint carrying two different pin maps are both detected (neither occurs here) rather than resolved by last-write-wins |
| `pcb_features.fixed` is unconstrained `text` that **nothing reads** | the importer sets no `fixed` on a mounting hole: a value there would claim a freeze that does not exist. Same inertness `pcb-keepout-does-not-bind.md` reports for `ftype='keepout'` |

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
- ~~**Decided (design, 2026-09-29):** `--copper` defaults to `none`~~
  **SUPERSEDED (Reto, 2026-09-30): there is no `--copper=fixed` to
  default away from.** "We extract the points and fix our model to match,
  then regenerate. No need to build." Imported copper is an input to
  correcting the spec, not geometry to keep — see §In scope. The 2026-09-29
  reasoning (frozen forever, does not follow a moved part, `op='rip'`
  clears the sketch not fixed rows) is why, and it argues for not building
  the write path at all rather than for defaulting it off.
- **Decided (Reto, 2026-09-30): freezing is EXPLICIT, never a default, and
  it must actually bind.** "All should be able to be frozen — nuts, holes
  etc for sure. But also parts — a part may be tall and in the corner
  because there is space there in assembly. But only actually freeze when
  needed." Two consequences: (1) the import carries the source's OWN lock
  state (`COMPONENT.locked` → `fixed='both'`) and freezes nothing else, so
  the planned `--freeze=all` default is dropped; (2) `pcb_features.fixed`
  has to stop being inert, because a frozen hole that nothing enforces is
  the freeze the user asked for and did not get — filed as
  `pcb-freeze-mechanicals-and-parts.md`.
- **Decided (Reto, 2026-09-30): free pads need a real model.** The spike
  board's 6 plated free pads on `GND` are measurement points ("just to
  measure. we should add a free pad model"). Today the import drops them
  with a warning because precis has no free-pad concept at all — filed as
  `pcb-free-pad-model.md`. Until it lands, those nets lose those
  connections on every import.
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
- ~~**Open (Reto):** whether the router is usable at this size is
  unmeasured.~~ **MEASURED 2026-09-30 at Reto's request, and the answer is
  no: 8 of 89 nets realized (9%)** in 105 s at `iters=200` — 54 nets
  `no_path, same-layer-crossing`, 27 `no_path`, and the job reported ZERO
  failures while doing it. Filed as
  `pcb-router-fails-at-real-board-size.md`; the harness is the
  `slow`-marked, env-gated `test_a_real_board_routes_at_all`. This bears
  on the whole thread, since re-routing is why the board is imported at
  all.


## gr470192 — routed intake preservation (2026-10-06)

Reto/Claude now requests routed-source preview, superseding the earlier
2026-09-30 regenerate-only policy for fresh intake. Existing reasons above
remain historical: source copper constrains later re-placement, so it must
be marked authored/fixed, never pretend to be a regenerated route/sketch.

Premise: dogfood-r13-intake-preview-v1 (pcb468457/board11) was authored as
a six-component synthetic netlist with local footprints; its recorded
setup has no epro import or source copper. R13 accepted pin-map preview,
not copper import. Empty copper on that fixture is expected from that
setup; no source-file association can be inferred from its name. Separate
actual importer defect: LINE and ARC parse/transform correctly, but
import_epro deliberately sends neither tracks nor vias to the store.

Implementation: fresh import_epro defaults to copper=fixed, CLI
import-epro --copper fixed|none (none retains regenerate-only intake).
Existing --update remains conservative copper=none by default; fixed
update refuses before writes because its partial netlist/outline update
cannot safely attach changed source copper. No automatic fixture repair.
Preserve each accepted source LINE/ARC as one fixed track row, with exact
converted width, layer, net, line/arc segments, centre and handedness.
Ordinary through vias use the existing extraction too. Invalid/unsupported
source records retain explicit existing warnings rather than fabricated
geometry. No TRACK alias inferred: this spiked epro2 format uses LINE/ARC.
Use existing pcb_fixed_copper_put in the SAME import transaction, after
net/stackup creation, with reserved __epro_source owner and source hash
provenance. No schema/IR/sketch/router changes, providers or routing.

Acceptance: synthetic tiny source with one straight and one arc record
persists exactly two fixed track rows; geometry/net/layer match source
conversion, real board SVG emits arc, pinout-preview read changes no
copper and proposed signal remains unsaved. Synthetic through via persists;
explicit none and dry-run write no fixed copper. Injected copper-write
failure rolls back new ref/design/footprints/board. Existing slug and
source-part pose protections retained; no route-success/DRCclean claim.

Indexed native source lives in serving /src (/app runtime), not this
isolated pcb-intake-copper tree, based on verified e77f51e0 R15. Native
Python search+outline preceded targeted local reads. Source review/root
normal land precedes exact deployed replay. Real .epro2 remains local;
local source path/board UUID for pcb468457 requested, absent from original
synthetic setup. Same-board dogfood replay cannot prove source preservation
until that association is supplied. Ready inbox/pcb-intake-copper-ready.md.

Explicit source copper net names absent from NET declarations are added to
fresh fixed intake's net table, without inferred pin connections. Measurement-only
and partial-update net handling remain unchanged. The store's unknown-net check
and enclosing rollback stay authoritative.
