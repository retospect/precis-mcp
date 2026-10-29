---
id: precis-pcb-footprint-help
family: pcb
title: precis — inspect one instance's footprint graphics (view='footprint')
summary: the read-only footprint-graphics view on a pcb design — get(kind='pcb', id='<slug>#<REFDES>', view='footprint') lists every non-pad primitive (silk/courtyard/fab/doc lines, arcs, polygons, regions, text) in footprint-local and board mm; keepout regions are inspectable but bind nothing yet; cached pre-graphics rows decode raw lazily and report dropped classes
answers:
  - how do I inspect one part's footprint graphics on a pcb design?
  - what does view='footprint' return — silk, courtyard, keepout regions?
  - why does view='footprint' say geometry: unavailable or dropped at import?
applies-to: get (kind='pcb', view='footprint')
status: active
tags: [design]
kinds: [pcb]
---

# precis-pcb-footprint-help — read one instance's footprint graphics

`view='footprint'` is the read-only inspect view for one placed
instance's non-pad footprint primitives — what an import decoded and
stored, in numbers, never a render.

## Inspect one part's footprint graphics

```python
get(kind='pcb', id='sensor-node#J1', view='footprint')
```

One instance only — the `#REFDES` selector, as pinout. Lists every
non-pad primitive — silk/courtyard/fab/doc lines, arcs, polygons,
regions, text — one row each: `kind`, `layer`, `keepout`, `width_mm`,
`local_mm` (footprint-local, mm, +Y up), `board_mm` (pad transform;
`unavailable` while unplaced), `text`. A `keepout='yes'` region is the
footprint's own rule area (an antenna "no copper" zone): inspectable,
binds nothing yet. Rows cached before graphics existed decode `raw`
lazily — body says `decoded from cached raw`; undecodable classes are
listed under `dropped at import:`. No stored row → `geometry:
unavailable` + nextcall. Read-only.
