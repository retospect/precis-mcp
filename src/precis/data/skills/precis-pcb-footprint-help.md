---
id: precis-pcb-footprint-help
family: pcb
title: precis — inspect one instance's footprint graphics and pads (view='footprint', view='pinout', view='pinout-preview')
summary: the read-only per-instance inspect views on a pcb design — get(kind='pcb', id='<slug>#<REFDES>', view='footprint') lists every non-pad primitive (silk/courtyard/fab/doc lines, arcs, polygons, regions, text) in footprint-local and board mm; keepout regions are inspectable but bind nothing yet; cached pre-graphics rows decode raw lazily and report dropped classes. view='pinout' reads the stored physical pads (IDs, local/board mm, pin/net mapping evidence); view='pinout-preview' echoes a proposed unsaved pin→pad mapping
answers:
  - how do I inspect one part's footprint graphics on a pcb design?
  - what does view='footprint' return — silk, courtyard, keepout regions?
  - why does view='footprint' say geometry: unavailable or dropped at import?
  - how do I inspect one connector's actual pads (view='pinout')?
  - how do I preview an unsaved pin→pad mapping (view='pinout-preview')?
applies-to: get (kind='pcb', view='footprint'/'pinout'/'pinout-preview')
status: active
tags: [design]
kinds: [pcb]
---

# precis-pcb-footprint-help — read one instance's footprint graphics and pads

`view='footprint'` is the read-only inspect view for one placed
instance's non-pad footprint primitives — what an import decoded and
stored, in numbers, never a render. `view='pinout'` and
`view='pinout-preview'` are the pad-level siblings on the same
`#REFDES` selector.

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

## Inspect one connector's actual pads — `view='pinout'`

```python
get(kind='pcb', id='sensor-node#J1', view='pinout')
```

One instance only; the selector is required. The ordinary `#J1` hop stays
the logical pin/net/neighbour view. Pinout reads cached or design-local
physical pads without fetching or changing the board. It shows original
pad IDs, distinct indexed rows for duplicate IDs, local/board mm, side,
rotation, layers and stored pin/net mapping evidence. `explicit-pin-pad`,
`footprint-pin-map` and `pad-number-identity` identify the mapping source;
identity fallback is not an independently known signal. `unconnected`
means a mapped pin without a net; `unclaimed` means no declared pin;
`ambiguous` retains conflicting pins/nets rather than choosing one.
A footprint with non-plated holes (pegs, standoffs) gets a second table,
one row per hole: `local_x_mm`/`local_y_mm`, `board_x_mm`/`board_y_mm`
(same transform as pads) and `dia_mm`; it is omitted when there are none.

Local +X is right and +Y up; board coordinates are top-view, rotations CW.
Bottom-side pads mirror local X before rotation and translation. Unplaced
instances retain local geometry but have unavailable board coordinates.
Missing cached/authored geometry gives an explicit nextcall, never guessed
pads. Catalog cache source versus authored geometry is stated; neither
verifies the supplier pinout or mating orientation. No provider pull, job,
catalogue refresh, routing or placement runs from this view.

## Preview an unsaved explicit mapping — `view='pinout-preview'`

```python
get(kind='pcb', id='sensor-node#J1', view='pinout-preview',
    args={'pins':[{'name':'CLK','pad':'2'}, {'name':'UNKNOWN','pad':None}]})
```

Existing instance/stored geometry required. This read-only echo adds proposed
labels beside P1's stored evidence; it never writes pins, assigns nets, fetches
geometry or queues jobs. `name` is a semantic label, not a net declaration.
Only `pins` is accepted; each entry requires exactly name/string and
pad/exact-string-or-null. Null means unknown binding; even the label NC does
not establish disconnection. Mating orientation and vendor numbering remain
unknown. Conflicting drafts are shown with canonical-authoring refusal hints,
never selected or persisted. This includes one name with both null and bound
pads; distinct null-bound names stay unknown, not conflicting. `pins=[]`
explicitly means no proposed assignments. Missing geometry stays unavailable with a manual
inspection/authoring hint. Duplicate physical pads remain distinct rows.

Limits: one instance,32 proposed entries,64 physical rows,64-character names,
32-character pad IDs,16384 response characters. Oversize requests return typed
correction, never silent truncation. Ordinary `view='pinout'` reads persisted
evidence only; use that view for larger stored instances.
