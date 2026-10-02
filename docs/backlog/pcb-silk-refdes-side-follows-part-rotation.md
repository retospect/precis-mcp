# pcb refdes silk picks its side in the part's frame, so a rot-180 column flips

Reto on heater-base-test (2026-10-02): "the labels on R19, 14, 12, 11 seem
on the wrong side (some rotation thing) given R51. R23 is different yet
again."

- `silk.py::_refdes_candidates`, `_bottom_edge_candidates` and
  `_below_box_candidates` express every candidate in the instance's local
  frame. A rot-180 0402 therefore gets its "below" label above it on the
  board. For a 2-pad part that is symmetric under 180°, rot 0 and rot 180
  are the same part, so which side its label lands on is arbitrary.
- On the board, R11/R12/R13/R14/R19 (rot 180, a 1.38 mm pitch column at
  x=37.719) land at `below-left-flush`, while R51 (also rot 180) lands at
  `ring 3 at 0deg`. Each of those labels also breaks the next part's
  courtyard outline ("courtyard outline broken around R8 refdes silk").
- R20/R21/R22 (rot 0) land at `ring 1 at 0deg`. R23, the column's last
  part, lands at `below-center` because R22's label already took its spot
  on the right. Placement is greedy per part, with no consistency across
  a row or column.
- EasyEDA's own designator poses (the ATTR records) are not imported, so
  the import has no authored label position to fall back on.

Fix direction: choose the label side in the board frame, normalising the
rotation mod 180 for parts that are symmetric under 180°. Give an aligned
row or column of identical parts one shared side.

test: a column of rot-180 0402s at 1.38 mm pitch gets every refdes on the
same board side, none inside a neighbour's courtyard. Mixing rot 0 and
rot 180 in that column changes nothing.
Thread: threads/ewod-pcb.md (silk was handed over from
pcb-easyeda-round-trip).
