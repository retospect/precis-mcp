# pcb refdes labels: an aligned row of identical parts gets no shared side

Reto on heater-base-test (2026-10-02): "the labels on R19, 14, 12, 11 seem
on the wrong side (some rotation thing) given R51. R23 is different yet
again."

Fixed, in the same commit that rewrote this item: label spots are now
picked in the board frame (`silk.py::build_silk`, module docstring "Read
from one side"), so a rot-180 part no longer puts its "below" label above
itself, over its own body. A 2-pad part now labels identically at rot 0
and at rot 180.

Still open:
- R20/R21/R22 (rot 0) land at `ring 1 at 0deg`. R23, the last part in
  that column, lands at `below-center` because R22's label already took
  its spot on the right. Placement is greedy per part, so nothing keeps a
  row or column of identical parts on one side.
- In a 1.38 mm-pitch column of 0402s (R7/R8/R11–R14/R19 at x=37.719),
  each label still breaks the next part's courtyard outline ("courtyard
  outline broken around R8 refdes silk").
- EasyEDA's own designator poses (the ATTR records) are not imported, so
  there is no authored label position to fall back on.

Fix direction: before the greedy pass, group instances that share a
footprint, rotation mod 180 and an aligned axis. Pick one spot that is
free for the whole group, and only then fall back per part.

test: four identical 0402s in a 1.38 mm-pitch column all get the same
spot name, and no label crosses a neighbour's courtyard.
Thread: threads/ewod-pcb.md (silk was handed over from
pcb-easyeda-round-trip).
