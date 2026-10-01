---
status: idea
title: T-slot aluminium profiles as real parts — standard cross-sections, slot geometry and the fasteners that ride in them
pillar: 3d-design
prio: normal
---

# T-slot aluminium profiles as real parts

Reto, 2026-10-01 (pillar review): add T-slot profiles to design, to the
DIN standards where they apply.

## Today

`src/precis/cad/catalog.py` resolves `extrusion:<profile>x<length>` for
three profiles (`2020`, `2040`, `4040`) as a bare `(w, d)` box
(`_EXTRUSIONS`, `_extrusion`). There is no slot, no groove, no bore and
no slot-size series, so nothing can be fastened into the slot and a
render shows a square bar. `se-off-the-shelf-fabrication.md` already
names "a captive T-slot at the far end" as one screw-joint outcome; this
item makes that outcome buildable.

## Wanted

- **Cross-sections as geometry**, by profile system and slot size (the
  20-series slot 6, the 30/40/45-series slot 8 and slot 10 families the
  common vendors share), with the slot opening, groove depth and core
  bore as parameters; standard references recorded per row (DIN 650 for
  T-slot dimensions, DIN 508 for T-slot nuts) with the vendor catalogue
  as the source where no standard governs. Every dimension cites a
  source; none from memory.
- **The fasteners that ride in the slot**: T-nuts and drop-in/roll-in
  nuts, hammer-head nuts, button-head and low-head screws, end-tapping
  of the core bore for end connections.
- **Joint propagation**: a screw that ends in a slot resolves to a
  slot nut of the matching size and seats it, the same way a tapped
  hole or nut pocket does today.
- **Brackets and connectors**: corner brackets, gussets, end caps, and
  the standard inside/outside corner connectors, as catalogue parts.
- **Cut list output**: profile, length, end machining, so a frame
  exports as an order.

## Owner

se-machine-design thread (catalogue parts feed the se assembly). First
consumer: the 19-inch rack helper (`rack-19in-helper.md`), whose frame
is the natural T-slot test piece.
