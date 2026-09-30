---
status: draft
title: "pcb: write an EasyEDA Pro .epro2 a colleague can open and keep working in"
prio: high
model: opus
pillar: 3d-design
---

# `pcb` → EasyEDA Pro `.epro2`

## Motivation / why

Reto (2026-09-29): a design iterated in precis has to leave, periodically, as a
file someone who has never used precis opens in EasyEDA Pro and keeps working
in — "a degraded but familiar workflow for arbitrary people". Today the only
outputs are Gerber + Excellon (fabbable, not editable — no nets, no components),
BOM/CPL, a KiCad *netlist*, a Specctra `.dsn`, and SVG. `.kicad_pcb` was
deliberately demoted (`gerber.py`'s docstring: Gerber X2 replaced it as the
critical path), and routing through KiCad to reach Pro adds a lossy hop plus a
version pin (Pro's docs say KiCad 5.1+, re-poured on import).

Writing Pro's own format is viable, and the 2026-09-29 spike made it cheaper
than feared. Pro 3.2 exports **`.epro2`**: a ZIP of `project2.json` plus one
`*.epru` stream carrying every document, each record a pair of named-key JSON
objects. So there are no positional `unk` slots to get wrong, `ARC` is a real
record (arcs need not degrade to chords), `ATTR`/`STRING` are real text
(designators need not ship as Hershey strokes), and layers are self-describing.
The reader half is shipped and its module docstring in
`src/precis/pcb/epro.py` is the format record; the writer emits what it
observed.

Shares the (now resolved) spike table below with
[`pcb-epro-import`](./pcb-epro-import.md).

## In scope

- `src/precis/pcb/epro.py` gains the write half — sibling of `gerber.py`, not an
  addition to `export.py` (whose contract is "IR in → text out" over
  `export_model`, which carries no geometry). One impure edge,
  `zip_epro(files) -> bytes`, mirroring `gerber.zip_fab`.
- `view='epro'` in `handlers/pcb.py`, writing `<slug>.epro2` into the
  established `_export_dir(slug)` convention.
- Named, tested, documented frame conversions in the style of the existing
  `export.jlc_rotation`: `to_mils`, `to_epro_xy` (+Y up → +Y down,
  origin-translated so every coordinate is positive), `epro_rotation`.
- **Deterministic** UUIDs (`uuid.uuid5(ns, f"{slug}:pcb")` …) and per-document
  record ids: this file is emitted repeatedly, so a re-export must re-open as
  the *same* project, and byte-comparison tests must mean something.
- Mapping: instances → `COMPONENT` + designator `ATTR` + `PAD_NET`; nets →
  `NET` (declared before use); tracks → `LINE`, arcs → `ARC`; vias → `VIA`;
  pours → `POUR` outline; outline → `POLY` on the `OUTLINE`-typed layer; silk strokes → `POLY`
  on layers 3/4.
- **Refuse synthesized pads by default**, importing `gerber.SynthesizedPadError`
  rather than defining a second one — the colleague's likely next action is to
  order the board, and a land-pattern bound solders to nothing.
  `args={'allow_synthesized': true}` permits it *and* writes a `STRING` on a
  documentation layer naming the affected refdes, because our Response text does
  not travel with the file.

### Prerequisite (slice 2a, landed 2026-09-29)

`_render_gerber` was ~180 lines of model assembly plus ~15 of write. Extracted
to `PcbHandler._fab_model(ref_id, *, slug) -> tuple[model, warnings] | None`,
the ONE assembly of `{layers, outline, copper, pads, drills, silkscreen,
soldermask_expansion_mm, mask_open_regions, instances}` — a second consumer
re-deriving pads, synthesized-pad fill-in, mounting-hole drills and the outline
fallback would be the same "one rule, N call sites, drifted" defect
`realize.pads_for_ir` and `padplace.pad_label` each already record. `instances`
is the one additive key; `gerber.py` ignores keys it does not know.

## Explicitly NOT in scope

- A synthesized schematic sheet. Emit **no** `SCH`/`SCH_PAGE` document (fall back to a single
  *blank* sheet only if the spike says Pro refuses a PCB-only project). A crude
  auto-placed schematic invites "update PCB from schematic", which would rewrite
  the netlist and destroy the board — an empty sheet is obviously empty, a fake
  one is a trap. The response says so every time.
- `POURED`. Our pour polygon is the realized *fill* boundary, not the authored
  zone; Pro re-pours on import (its own docs warn the result differs). Emit the
  `POUR` rule and say so.
- Blind/buried via spans. `VIA` is documented as through-hole only; emit
  through-hole and warn loudly per via rather than silently flatten.
- Per-pad `role`/`mask`/`paste` intent, `mask_open_regions` (the EWOD case), and
  `polarity: "clear"` silk draws — no verified Pro target; warn and drop, never
  silently (dropping a mask-open region ships a board whose electrode field is
  mask-covered).

## Acceptance criteria

Per slice; the real one is a human opening the file.

1. **2b — smallest file Pro opens.** `project2.json` + one `.epru` whose
   `PCB` document carries `LAYER`, `NET`, the outline `POLY`, `COMPONENT` +
   `ATTR` + `PAD_NET`, followed by one `FOOTPRINT` document per
   instance. Bottom-side instances are **in** from the start now that R1 is
   closed — the mirror axis is known (Y) and pinned by a test, so refusing
   them would cost more than it protects. *Done: a human opens it in EasyEDA
   Pro and sees the outline with every part at the right spot with the right
   refdes, including the bottom-side ones.*
2. **2c — copper.** `LINE`, `ARC`, `VIA`. *Done: Pro's unrouted count equals
   `view='route-status'`; clicking a trace shows the right net.*
3. **2d — pours.** *Done: re-pour in Pro succeeds; visual compare against
   `view='svg'`.*
4. **2e — silk + editable designators.** Courtyard/pin-1 `POLY`, designator as
   `ATTR`/`STRING` **with its stroked twin suppressed from the `POLY` stream**.
   Needs the label anchor, which `SilkPlacement` does not carry — add
   `x/y/angle` (additive, defaulted; `drc.check_silk_missing`/
   `check_silk_printability` read the census and are unaffected) rather than
   dumping the designator at the component origin and losing the placement
   `silk.py` worked to compute. *Done: designators are selectable, editable text
   at the same place our gerber prints them; no doubled labels.*
5. **2f — deferred set.** Per-distinct-footprint `FOOTPRINT` document with a
   real `angle` (R1 is closed, so this is now a size/tidiness change rather
   than a correctness one), rule areas, `mask_open_regions`, NPTH holes,
   teardrops — each either round-trips or is a documented, warned-about drop.

Automated (structural, `test_pcb_export.py` house style, not byte-golden): zip
integrity; every `.e*` line a JSON array with a known type and declared arity;
referential integrity (every net declared before use, every `PAD_NET` resolving
to a real component and a real pad, every layer declared, every `ATTR.parentId`
resolving); determinism (same model → identical bytes, twice); the
synthesized-pad refusal; each named warning fires. **The test that would
actually catch a frame bug:** a pad at a known board position, re-derived from
`COMPONENT` position + `FOOTPRINT` local pad position + the declared orientation
convention, lands within one quantum (0.5 µm) — this is what makes the
baked-orientation choice safe. Once the reader exists: a full
`epro → reader → fab model` round trip with set equality on
nets/pads/components.

## Target + blast radius

`src/precis/pcb/epro.py` · `src/precis/handlers/pcb.py` (`_render_epro`,
`_EXPORT_VIEWS`) · `src/precis/pcb/gerber.py` (model docstring documents the
`instances` key) · `src/precis/pcb/silk.py` (`SilkPlacement` gains `x/y/angle`
in 2e) · `tests/test_pcb_epro.py`. No migration, no new dependency (stdlib
`zipfile` + `json`).

## Open questions / decisions log

- **Decided (Reto, 2026-09-29):** native `.epro2` writer, not a KiCad hop and not
  Gerber-only. EasyEDA Pro is the target editor.
- **Decided (design, 2026-09-29):** one `FOOTPRINT` document **per instance** in the first
  cut, with rotation and mirror **baked into the pad coordinates** and
  `COMPONENT.orientation = 0`. We already have every pad in absolute board mm
  via `padplace.board_pads`; subtracting the centroid gives a local frame
  correct by construction regardless of how Pro reads `orientation`. A wrong
  orientation convention produces a board that looks plausible and is
  unbuildable — the exact failure `padplace.py` exists to prevent. Cost: the
  colleague sees `R1_0402`, `R2_0402` … as distinct library footprints.
  **Update 2026-09-29:** R1 is closed, so the baking is no longer load-bearing
  for correctness — keep it for slice 2b anyway (it is strictly less to get
  wrong), and promote to per-distinct-footprint in 2f, where the real board's
  33 footprints across 140 components show what it buys.

### The spike table (shared with `pcb-epro-import`) — RESOLVED 2026-09-29

Reto exported a real 140-component 4-layer board (`heaterBaseTest.epro2`,
EasyEDA Pro editorVersion 3.2.149). **It is `.epro2`, not the `.epro` KiCad's
dev-docs describe** — same record-type vocabulary, different container and a
different record encoding. See
[`pcb-epro-import`](./pcb-epro-import.md)'s "Spike result" section for the
full table; the findings are in `src/precis/pcb/epro.py`'s module docstring in
`easyeda.py`'s house style, and the reader is shipped.

The consequence for THIS item: bodies are named-key JSON objects, so **the
`unk` positional fields that were the entire risk surface of the write side do
not exist**. Emitting a record means emitting an object with the keys the
reader observed, and an omitted optional key is visibly omitted rather than
silently occupying a slot that means something else.

| id | status |
|---|---|
| S1 | **open** — export-side; does Pro open a project whose stream has no `SCH`/`SCH_PAGE` document |
| S2 | **closed** — `project2.json` has 6 keys (title, cbb_project, editorVersion, introduction, description, tags); no document maps at all |
| S3 | **closed** — no `HEAD`/`DOCTYPE` record. `type:"DOCHEAD"` opens each document and carries docType/uuid/editVersion/updateTime/user/version |
| S4 | **closed** — an `ATTR` with `key:"Footprint"` and `parentId` = the component id; `value` is the FOOTPRINT document's uuid. 33 footprints shared across 140 components |
| S5 | **moot** — no positional fields |
| S6 | **closed** — `viaType` + `unusedInnerLayers`, no `layerId`. All 234 spike vias are `NORMAL`/empty, so blind/buried remains unobserved and the reader refuses rather than flattening |
| S7 | **closed** — `hole:{holeType,width,height}` + `defaultPad:{padType,width,height,radius}` + `specialPad[]`, `plated`, `padOffsetX/Y`, `relativeAngle` |
| S8 | **moot** — there is no `REGION`. Outline is a `POLY` on the `OUTLINE`-typed layer; rule areas are `RULE` + `RULE_SELECTOR` records |
| S9 | **closed enough** — `POUR` carries `name`/`order`/`pourType`/`keepIsland` and a `path` of shape ops; `POURED` is the separate cached fill and one spike `POURED` has an empty `pourFill`, so a pour without fill is valid. ⚠ `POURED.pourFill` is in **10-mil units**, unlike everything else |
| S10 | **closed** — designators are `ATTR` with `key:"Designator"`, `parentId`, `x`/`y`, `angle`, `origin`, `fontSize`, `strokeWidth`, `mirror`, `valueVisible`. `STRING` is free text (10 on the spike board, all on silk) |
| S11 | **closed** — mils, Y down. `CANVAS.originX/originY` is the user's origin and was `(0,0)` here, but `Frame` derives the transform from the outline rather than trusting it |
| S12 | **moot** — no `.efoo` members; footprints are `DOCHEAD docType:"FOOTPRINT"` documents in the one stream |
| R1 | **closed** — `component (x, y) + R(+angle) · (px, ±py)`, Y-down frame, bottom-side mirrors in **Y**. Settled by net agreement: 573 same-net endpoint/pad hits, **zero** cross-net; all five other candidates disagree. Unblocks bottom-side export and per-distinct-footprint documents |
| R2 | **open** — `.epro2` is what Pro 3.2 exports; whether an `.eprj` offline variant still exists and which the handoff should emit is untested |
| R3 | **open** — 47 `SYMBOL` documents with 421 `PIN` records are present, but the `DEVICE`→`SYMBOL` chain has not been walked |

Still wanted from Reto for the write side: a deliberate throwaway test project
(one THT part, an asymmetric **bottom-side** part, a routed net with an **arc**,
a via, a GND pour, a silk polygon, a rule area) — the real board has no `ARC` on
its PCB and no bottom-side part whose mirror the import could not already
settle, so those two remain unexercised on the way out.
