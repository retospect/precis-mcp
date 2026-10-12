---
status: draft
title: EWOD non-fab mechanical sheets — what is left after mech2d v1 (gr451662)
pillar: 3d-design
---

# EWOD non-fab mechanical sheets — what is left after mech2d v1 (gr451662)

The EWOD stack needs 2D layers that no PCB fab makes: the ITO top-sheet
cut, the spacer adhesive (a frame with the array window, optional via-plaza
covers) and shared alignment features. The output goes to a laser cutter
or Cricut. Architected by Reto 2026-09-26; ruled 2026-10-12 (design-fold).

**Shipped (v1, b898944ce).** `src/precis/pcb/mech2d.py`, pure and never in
`export_fab`, provides:
- the ITO cut and the spacer adhesive;
- the shared round-hole plus slot alignment primitive;
- kerf and mirror applied at export;
- a board-fingerprinted SVG;
- the connected-component check.

**Left.**
1. Stencil bridges for interior islands, as open-polyline gaps that avoid
   the copper keepout. The connected-sheet check must still pass, so the
   adhesive stays one piece.
2. The channel-cutout style variant.
3. A coating-extent input (gr414481).
4. Wire a view. **Do not reuse `view='mechanical'`:** it is the existing
   JLCPCB fab view (`handlers/pcb.py::_EXPORT_VIEWS`,
   `pcb_export.mechanical_profile`). Use `view='cutsheets'` calling
   `mech2d.export_mechanical`. Wiring it removes `export_mechanical` from
   `tests/test_pcb_dead_exports.py::_KNOWN_UNWIRED`.
5. Persist the layers as generator rows, so a re-put regenerates them.

**Needs Reto (dimensions).** All of these stay named placeholders in
`MechSpec` until supplied:

| Dimension | Proposed value |
|---|---|
| ITO inset | 0.5 mm |
| Window margin | +1 mm |
| Alignment hole and slot sizes | none proposed |
| Mirror default | off |

**Later (Reto: "later").** A parylene-liner-edge note and a Cricut importer
check.

**Accept.** `get(kind='pcb', id='ewod-dogfood-6', view='cutsheets')`
returns an SVG per sheet, each with the board fingerprint. The adhesive
sheet passes the connected-component check with bridges present, and no
bridge crosses copper keepout.

Owner: `threads/ewod-pcb.md` Horizon 10. The sheet-job adapter for flat-pack
(se-machine-design-7) consumes these sheets.
