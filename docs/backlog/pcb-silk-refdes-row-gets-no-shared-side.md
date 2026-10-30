---
status: idea
prio: low
pillar: 3d-design
---

# pcb refdes labels: what is left after the shared-side fix

Reto on heater-base-test (2026-10-02): "the labels on R19, 14, 12, 11 seem
on the wrong side (some rotation thing) given R51. R23 is different yet
again."

Both halves of that are fixed. Label spots are picked in the board frame
(`silk.py` module docstring, "Read from one side"), and an aligned row or
column of identical parts now shares one spot (`_aligned_label_groups`,
module docstring "A row of identical parts shares one label spot"). On the
board's real geometry (prod ref 460559: R51, R1, R7, R8, R11–R14, R19 at
x=37.719, R0402 pads from `pcb_local_footprints`) all nine take
`ring 1 at 0deg`. R3, further down the same x, keeps `below-center`: C15
sits between it and R51, so it is not part of the run.

Still open:
- "Courtyard outline broken around R8 refdes silk" did NOT reproduce on
  the real pads after the board-frame fix (no courtyard relocated in the
  column, same probe). Re-check on the next render of the board before
  spending anything on it. The one silk loss the board's DRC still reports
  in that area is R51's courtyard dropped against C15's, and DRC also
  reports C15 and R51 courtyards overlapping. That is placement, not silk.
- R20–R23's courtyards overlap each other by 0.112 mm (DRC
  `courtyard_overlap`). That is the placement too, and the column fix
  cannot help it.
- EasyEDA's own designator poses (the ATTR records) are not imported, so
  there is no authored label position to fall back on.

## Tip-cluster label walks — shared walls leave no ring gap (gr538005)

After the 2026-10-29 label-room campaign (capability-priced SilkRoom,
flat edge axis, label axis + `settle_label_room` repair, pins, route
keepouts — runs 35–45), the taz-leaf far-label count settles at ~28–32
of 50 (measure: refdes anchor >3 mm off the reservation bbox). The root
cause is measured, not guessed: 76 % of blocked label candidates fail
against *shared walls* — between two parts at pair-floor distance the
settled outline is one wall at the hull midpoint, and a ~2 mm label box
needs ring gap ≈ anchor + clear + text ≈ 3.5 mm to fit between. Labels
can only live on a cluster's outer perimeter, and the tip cap array —
boxed in by the two `fixed='both'` comb electrodes SNS1/SNS2 — has a
short perimeter. Effort is not the lever (40k evals ≈ 20k). The
3.1–3.9 mm bucket is mostly corner-spot labels that read adjacent; the
true walks are the >4.5 mm dozen. R23's original complaint (4.9 mm +
squished on the outline) is fixed by the campaign; this remainder is
geometry, not scoring.

Open — Reto's pick (`reto:ask`):

- (a) upgrade the repair-pass oracle from the box model to the real
  `build_silk` census — the model can never reproduce settled outlines
  in dense clusters (runs 42/43 proved model-clean poses still walk at
  draw time);
- (b) silk-side cluster labelling — one label per shared-wall cluster;
  a draw feature, not placement;
- (c) board-script area allocation for the tip cluster.

Thread: threads/ewod-pcb.md (silk was handed over from
pcb-easyeda-round-trip).
