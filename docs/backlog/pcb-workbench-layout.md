---
status: ready
title: pcb web workbench — schematic left, layout right, one chat, cross-probe
prio: normal
model: opus
pillar: 3d-design
---

# pcb web workbench: two panes, cross-probing, one chat

Reto's design pass on `/pcb/usbc-attiny-dogfood-b`, 2026-10-18 ("spec it").
Today the page stacks the board render and the net-label schematic, and has
two text boxes. "Argue with the design" (`POST /pcb/{slug}/note`) is the Ask
box: question notes, answers, no edits. "Design chat" is the Propose box:
proposed edits, never applied without an explicit Apply. Only the board SVG
stamps `data-handle` (`precis.pcb.argue`), so clicking the schematic adds
nothing.

## Decided (Reto, 2026-10-18)

- Schematic on the left and layout on the right, using the full window
  width, with a draggable divider whose position is remembered per user.
  The info strip sits below both panes.
- One chat box, with an Ask / Propose toggle and a clear button. Clicked
  handles show as removable chips. Propose keeps the explicit Apply.
- Hover and click, Reto's rule: "when not hovering over interesting things
  it says 'selection' with details, when hovering it is temp replaced with
  hover".
  - Click selects: the info panel shows the selection's details and the
    handle joins the chat chips.
  - Hover over a part, pin, net, via or track temporarily replaces the info
    panel with that object's details, and highlights it in both panes.
  - Leaving the object restores the selection view. Hover never changes the
    selection or the chips.
- Cross-probing works both ways. A part, pin or net is one object with one
  handle in both panes. Click pin 4 of IC2 in either pane, and the pin, its
  net, every pin on the net and their parts light up in both.
- Power and ground nets (net class `power`/`ground`, or GND by name) light
  only the clicked pin, and the panel says "GND, N pins". The whole net is
  never flooded.
- Handle prefix: parts, pins and nets keep one grammar (`U1`, `U1.3`,
  `net:GND`). Objects that exist only in the layout carry `L:`: tracks,
  vias, pours, zones (`L:via 12`). A layout pin is not a different object
  from the netlist pin, so it gets no `L:` prefix.
- The layout gets a flip button: a bottom view, mirrored, with the B.* layers
  drawn on top.
- Zoom and pan come from one shared module for the pcb panes and se 2D. se 3D
  uses the same keys and adds rotate (table below). The module zooms by
  rewriting the SVG viewBox, so geometry and text stay crisp.

| Gesture | 2D (schematic, layout, se 2D) | se 3D |
|---|---|---|
| Wheel or pinch | zoom toward the cursor | zoom toward the cursor |
| Trackpad two-finger scroll | pan | zoom (orbit convention) |
| Drag on empty space | pan | rotate |
| Right-drag, middle-drag, or space+drag | pan | pan |
| Double-click on empty space, or `f` | fit to view | fit to view |
| `0` / `+` / `-` | reset / zoom in / zoom out | same |

The keys register through the shortcuts module (`static/shortcuts.js`,
`data-shortcut`), so `?` lists them.

## Proposed (builder decides inside these bounds)

- Part cards on hover in both panes show refdes, value, package, LCSC number
  and stock, plus a thumbnail. The thumbnail URL can't be built from the
  C-number. The server resolves it once from the part's LCSC or JLC page via
  `safe_get`, then caches it on the part ref (meta plus a cached image). The
  page shows the cached copy and never hotlinks. With no image, the card has
  no image.
- Focus mode on the schematic, the "fisheye" Reto asked about. It's entered by
  a button or a double-click, never by hover. It shows the selected part or
  net with its one-hop neighbours; everything else is dimmed but stays in
  place. Pin fixes a part in place ("I want esp32 to stay here"). Any
  re-layout is animated and happens only on an explicit action, because
  layouts that move on their own break the reader's mental map.
- Multiple sheets: split the schematic by the board's placement groups
  (power, MCU, USB-C, ...). Each sheet is a tab, plus an overview tab.
  Cross-sheet nets end in off-sheet connectors that name the other sheet.
  Cross-probing switches to the sheet that holds the object.

## Slices

Slice numbers are stable anchors (code comments cite them); don't renumber.

1. **Focus mode and pinning** on the schematic — *shipped*. Double-click a
   part or net label enters focus (`schematic.svg?focus=<handle>`): the
   target's one-hop neighbourhood stays bright, the rest dims in place
   (signal nets only — power/ground never flood). A part's placement lock is
   drawn as a pushpin and toggled by `POST /pcb/{slug}/pin`, surviving a
   reload. Not yet: animated re-layout on an explicit action (the schematic
   is still deterministic shelf-packing; pinning only constrains a future
   placement). Code: `precis.pcb.schematic`, `static/pcb-focus.js`.
2. **Multiple sheets.** Sheets come from placement groups, with tabs and
   off-sheet connectors. Exit: a schematic test that a cross-sheet net gets
   connectors on both sheets, and that cross-probing picks the sheet.
3. **se adopts viewport.js.** Same gestures and keys; 3D adds rotate. Exit:
   se 2D uses the module (static test), and the se 3D key map matches the
   table.

## Out of scope

- Editing geometry by dragging in the browser. Propose plus Apply stays the
  only write path.
- Live collaborative cursors.
