---
id: precis-pcb-ewod-help
family: pcb
title: precis — generate an EWOD electrode-array board (ewod_pad_array)
summary: generate a whole EWOD/digital-microfluidics electrode field — pads, neck stubs, via plazas, and a machine-readable capability map — from a few params via a generators block on put(kind='pcb'), instead of hand-authoring polygon pads one at a time. Covers grid/pads, variant, derived pitch/gap/via/hv_separation sizing, reserve slots, pad_sizes merges, tenting, and view='capability'.
answers:
  - how do I make an EWOD / digital microfluidics electrode array board?
  - how do I generate a pad field instead of authoring pads by hand?
  - how do I merge several EWOD electrodes into one bigger pad?
  - how do I see which pads and via slots a generated array actually uses?
  - why did my ewod_pad_array pitch/gap/via get rejected?
applies-to: put(kind='pcb', args={'generators': [...]}); see also kind='pcb'
status: active
---

# precis-pcb-ewod-help — generate an EWOD electrode field

An EWOD (digital microfluidics) board's top copper is a field of drive
electrodes with via plazas dropping escapes to the bottom layer.
`ewod_pad_array` emits the whole thing — pads, neck stubs, vias, and a
slot ledger — as ONE component from a handful of params, instead of a
polygon-pad-per-electrode authoring pass.

## Generate a pad field

```python
put(
    kind="pcb",
    id="ewod-board",
    args={"generators": [{"name": "ARR1", "generator": "ewod_pad_array", "params": {"grid": [9, 9]}}]},
)
```

- `grid: [rows, cols]`, or `pads: N` (N must be a perfect square for a
  square field — pass `grid` explicitly for a rectangle or a `rim`).
- `variant: 'full'` (default) or `'rim'` (perimeter pads only, hollow
  interior — "an 8x8 but just the outer rim").
- Every electrode is a pin on component `ARR1` (`R<row>C<col>`) — wire a
  driver to it like any other part's pin
  (`{"net": "CH0", "refdes": "ARR1", "pin": "R0C0"}`).
- Re-`put`ting the same `params` is a no-op; changed params replace the
  whole expansion (old instances retired, new ones inserted).
- A board authored by an older generator version keeps that version's
  copper until its generators entry is put again; `op='place'`/`'route'`
  never re-expand it. Put and place/route responses name such a board
  ("generator output is stale"). Putting the entry again re-expands it,
  and `"regenerate": true` on the entry forces a re-expansion even when
  nothing changed. Either way, the board's placement and routing are redone.

## Sizing is derived, not chosen

`pitch` (default 2.0mm), `gap` (electrode gap, default 0.10mm), `via`
(`{dia, drill}`), `hv_separation`, and `edge` (`{tooth_depth,
tooth_pitch}`) default from fab capability figures and interact — `put`
rejects a `pitch` under the derived plaza-capacity floor for the given
`gap`/`via`/`hv_separation`, naming the floor in the error. Widen
`pitch`, or loosen `via`/`gap`/`hv_separation` explicitly.

```python
{"grid": [9, 9], "pitch": 2.5, "via": {"dia": 0.5, "drill": 0.25}, "drive_voltage_v": 150}
```

`drive_voltage_v` derives a real HV clearance margin for the plaza
internals and bottom-layer escapes; omit it and `hv_separation` falls
back to the fab's own ordinary copper-spacing floor (the electrode-to-
electrode `gap` itself stays advisory either way — the top coating
carries that insulation).

## Via plazas — no via-in-pad, ever

Every third interior grid position is left pad-less (a "plaza"); its 8
neighbours each neck a short stub into it and drop ONE via to the
bottom layer. No via ever sits under a pad. Override the auto positions
with `plazas: [[r, c], ...]`.

## Withhold a slot for something else

```python
{"grid": [9, 9], "reserve": ["P1_1:N"]}
```

`reserve` names plaza slots (`P<row>_<col>:<N|S|E|W|NE|NW|SE|SW|C>`) to
withhold from electrode escape — the owning electrode is marked
unusable in the capability map rather than silently unrouted. Read
`view='capability'` first to find real slot names for a given board.

## Merge several cells into one bigger pad

```python
{"grid": [9, 9], "pad_sizes": [{"cells": [[0, 0], [0, 1]], "name": "RESERVOIR"}]}
```

- `cells` must form a solid rectangle (no L-shapes or holes); `name` is
  optional (defaults to the covered block's own top-left pin name, e.g.
  `R0C0`).
- The merged pad gets ONE net, ONE stub, ONE via — a reservoir/dispense/
  common electrode is just a bigger version of the same pad, wired the
  same way.
- A merge can never cover a via plaza (or, in `variant='rim'`, a hollow
  interior cell) — `put` rejects it and names the offending cell rather
  than relocating the plaza.

## Zigzag edges — arrays tile against each other

Every internal AND external edge is a complementary zigzag
(`edge.tooth_depth`/`tooth_pitch`), so two boards butt together with no
discontinuity at the seam. `external_edge: 'mesh'` (default) keeps that
profile on the outer boundary too; set `'straight'` for an edge that
will never neighbour another array.

## See what you actually got — `view='capability'`

```python
get(kind="pcb", id="ewod-board", view="capability")  # SVG schematic
get(kind="pcb", id="ewod-board", view="capability", args={"format": "ledger"})  # table form
get(kind="pcb", id="ewod-board", view="capability", args={"name": "ARR1"})  # pick one generator
```

Usable vs. unusable/reserved pads, each plaza's 8+1 slot allocation,
pin names, and the computed sizing figures — a legend for "which pads
can I actually drive," not exact fab geometry. `args={'name': ...}` is
required once a design has more than one generator call.
`view='svg' args={'level':'fab'}` is the render that matches the
exported gerbers bit-for-bit.

## Other params

`tenting: 'on'|'off'` (plaza vias, default `'on'` — no filled/capped
vias either way), `corner_radius` (recorded, not drawn — fab's own
corner rounding already does the job).

### `sink_grid` — bottom-side driver instances

Live, not a stub: it emits one bottom-side component per
`channels_per_sink` usable electrode escapes, wires each channel pin to
its electrode net, daisy-chains the sinks `DIN`->`DOUT`, and declares
each instance's channel pins as one pin-swap group. Needs exactly one of
`part` (an LCSC C-number) or `footprint` (a local footprint from this
same `put()`'s `footprints` block) — the generator wires the sink, it
never authors the sink's own geometry — plus `channel_pins`. Shares are
balanced by chain order, not by spatial block (`per_tiles` is removed
and refused by name).

`fixed: 'xy'|'rot'|'both'` (default: **omitted — the placer owns the
position**). Each sink is emitted at the centroid of its own share of
electrodes, but that is a SEED the annealer may leave. Set `fixed` only
when a sink's pose is a mechanical constraint rather than a routing
preference — the same reason nuts, screw holes, alignment pins and
board-edge connectors get pinned. Pinning a sink under the electrode
array is what put one board's solder lands on top of its own authored
plaza vias, so prefer the default.

## See also

```python
get(kind="skill", id="precis-pcb-help")  # netlist/placement/route surface the array's pins plug into
get(kind="skill", id="precis-pcb-route-help")  # op='route' -- escapes route like any other net once placed
```
