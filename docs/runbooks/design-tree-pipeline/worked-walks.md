# Three worked walks through the design tree (for Sonnet builders of S3 and S4)

Use these as the reference behaviour for `precis-se-dtree-help`, the se Next: hints and the skill
evals. Every rule id below exists in `<drafts>/{join,rotate,close,principles}.yaml`. The
objects are neutral on purpose; do not add unicycle or bicycle examples to skills (gr516957).

How every walk runs, in order:
1. List the interfaces (every pair of parts that touch or connect).
2. Name each interface's function in one word (join, rotate, close, ...).
3. Walk the function tree: answer the node's `asks`, pick the child whose `when` fits and whose
   `avoid_when` does not, stop at a leaf. Max 4-5 node reads per interface.
4. At the leaf: apply its rules, read every issue, then sweep the five axes (locking, captive,
   corrosion, bearing-face, material-direction): relevant -> choose, n/a -> say why.
5. Principles sweep on every part: retention (6 DOF, both directions), fail-safe, load path; and on
   the assembly: over-constraint, assembly and access, mistake-proofing, tolerance stack, thermal.
6. Record: one decision note per interface, `params.leaf`, one principles note per part, and a gap
   note wherever the walk ended on a shallow node or a red rule.

Rule confidence matters when you apply a number: `standard`/`handbook` use directly; `secondary`
use, and name the standard; `excerpt-only`/`unverified` use only as a starting point and record it
as provisional; `shop-rule` is guidance, never a DRC threshold.

---

## Example 1. Printed bracket screwed to an aluminium plate (join)

Task: a PETG bracket (desktop FDM) holds a small motor; it is fixed to a 5 mm aluminium 6061 plate
from the top only; it must come off for service; the object sits outdoors and vibrates.

**Interfaces.** (a) bracket to plate: join. (b) motor to bracket: mount, which is shallow today, so
it uses the same threaded leaf with the motor's own tapped holes; datasheet first.

**Walk (a).** `join` asks: separable=service, far_material=metal (aluminium), access=one-side,
environment=outdoor, vibration=yes, process: near part desktop-fdm.
- Integrate? No: two materials and it must come apart -> not `join/integrate`.
- `join/service` (separable=service) -> `threaded` (load plus vibration rules out pins and snaps).
- `threaded/clearance-tapped`: access one side, far part metal. Leaf reached in 4 reads.

**Rules applied (M4 chosen).**
- Clearance hole in the bracket: ISO 273 medium. Rows held for M3/M6/M8/M10/M12 (c1, c16) but not
  M4: look up M4 (4.5 mm) and record it as provisional, a gap in the leaf's table.
- Tap drill in the plate only (issue "tap-drill diameter used as clearance diameter").
- Engagement in aluminium 2.0 d (c4, secondary) = 8 mm. **The plate is 5 mm: fails.** The leaf's
  avoid_when catches it ("thin sheet below engagement length"). Options: rivet nut
  (`join/permanent/permanent-mechanical/rivet-nut`, also under threaded), a through bolt if the
  underside becomes reachable, or a thicker boss plate. Choose rivet nut; record why.
- Edge distance 1.5 d minimum, 2 d nominal (c6, handbook, NASA RP-1228): 8 mm from the plate edge.
- One screw lets the bracket pivot -> `locate/second-fastener`: each screw carries M/p (n1);
  spacing 4 d nominal (n2) = 16 mm or more; one round hole plus one slot if the holes are drilled by
  hand (issue "both screws in slots").

**Axis sweep.**
- locking: relevant. Split lock washers do not lock (clearance-tapped c8); preload first, plus
  medium threadlocker (axes/locking c4 size guide) on the steel thread in the rivet nut. Do not let
  threadlocker touch the PETG (rotate/.../bearing-in-printed-part c14: anaerobics attack some
  thermoplastics).
- captive: n/a, serviced on a bench.
- corrosion: relevant, always for metal pairs. Fastener should be the cathodic (noble) member and
  the small part on a large anode is the favourable case (axes/corrosion c3, c4): stainless A2
  screw in a steel or stainless rivet nut in a large aluminium plate is acceptable with a barrier
  compound; record the anodic-index difference (c2, NASA-STD-6012 limit 0.25 V).
- bearing-face: relevant. Screw head on PETG creeps -> washer, or a counterbore with a metal sleeve
  (axes/bearing-face issues).
- material-direction: relevant. Print the bracket so the bending arm lies in the layer plane.

**Principles sweep (bracket).** Retention: 6 DOF all stopped by two screws plus clamp; rotation
about the screw axis stopped by the second screw, not by friction. Load path: motor torque ->
bracket -> two screws in shear? No: shear goes into the plate by friction with real preload; check
slip (locate/second-fastener n8, n9: assume friction no more than 0.2) or add a shoulder against a
plate edge. Thermal: PETG on aluminium outdoors, check the two-screw spacing for differential
expansion; use the slot.

**Record.**
```
decision note dt-bracket-plate: join > service > threaded > clearance-tapped -> rivet-nut
  (plate 5 mm < 2.0 d engagement). Answers: separable=service, far=Al 6061, access=one-side,
  outdoor, vibration. Rejected: tapped Al (engagement), adhesive (service), snap (metal).
  Axes: locking=threadlocker medium; captive=n/a bench service; corrosion=A2 + barrier, fastener
  noble; bearing-face=washer; material-direction=arm in layer plane.
  Gaps: ISO 273 M4 row missing (provisional 4.5 mm).
params.leaf = join/permanent/permanent-mechanical/rivet-nut
principles note bracket: DOF table, slip check, slot for expansion.
```

---

## Example 2. Pulley shaft in a printed housing on two ball bearings (rotate)

Task: an 8 mm steel shaft carries a printed pulley; it turns continuously at moderate speed in a
PETG housing on two 608 bearings; light radial belt load; indoor.

**Interfaces.** (a) bearing outer ring to housing: rotate (seat). (b) shaft to bearing inner rings:
rotate (seat). (c) shaft axial position: rotate (retention). (d) pulley to shaft: rotate/shaft-hub.

**Walk.** `rotate` asks: motion=continuous, speed moderate, load radial light, play small,
process desktop-fdm housing -> `rolling-bearing` -> `bearing-in-printed-part` for (a);
`seat-and-retain/locating-floating-arrangement` and `axial-retention` for (b, c);
`shaft-hub/set-screw-on-flat` for (d) (light torque, printed hub).

**Rules applied.**
- Bearing size from the datasheet, never memory (principles/look-up-dont-assume): 608 is
  8 x 22 x 7 mm.
- Bore in the printed housing: not equal to the bearing OD (issue "CAD bore exactly equal to bearing
  OD"). Start from the printer's own hole test print (bearing-in-printed-part c3), Prusa accuracy no
  better than 0.2 mm (c1), elephant-foot compensation about 0.2 mm on the first layer (c4).
  Choose a coupon-calibrated slip fit plus a cover, or retaining compound on a degreased bore after
  a coupon test (c6, c14).
- Machined-seat precision (IT6 housing, IT5 shaft, c9) is not reachable as printed; do not claim it.
- One bearing locates, one floats (locating-floating-arrangement; issue "both bearings fixed while
  shaft expands"). Interference alone does not locate axially (c12): add a shoulder or cover.
- Axial retention both directions (axial-retention): printed circlip groove under thrust is weak ->
  shaft collar or a metal shoulder; a circlip on the steel shaft is fine for light thrust.
- Pulley on shaft: set screw on a flat, not on round (set-screw-on-flat issue). Flat-point is
  ISO 4026, cup point ISO 4029 (c1, c2). Set screw about 0.5 x shaft diameter (c3, secondary):
  M4 on 8 mm. PETG hub, not PLA (issue "PLA hub in a warm application").

**Axis sweep.** locking: relevant, the set screw under vibration -> threadlocker or a second set
screw at 90 deg (c6). captive: n/a. corrosion: steel shaft, steel bearing, n/a indoor dry (still
recorded). bearing-face: n/a (no clamp on plastic). material-direction: relevant, the bearing seat
must not sit across weak layer interfaces (bearing-in-printed-part issue): print the housing with
the bore axis vertical and extra perimeters (c15).

**Principles sweep (shaft).** DOF: rotation about its axis intended; axial translation stopped both
ways (collar one side, shoulder or circlip the other) at the locating bearing only; radial and tilt
stopped by two bearings. Over-constraint: the floating bearing slides in its seat. Assembly order:
bearings into housing, shaft through, collar, pulley; the set screw is reachable by a hex key
through a slot in the housing at its turn.

**Record.** Four decision notes (seat, arrangement, retention, hub), `params.leaf` per connect,
a principles note for shaft, pulley and housing. Gap: none; red rules used: interference formula
(c10, excerpt-only) only if a press fit is chosen.

---

## Example 3. Hinged lid with a magnetic catch and a splash seal (close)

Task: a printed PETG electronics box with a lid opened daily by hand, no tools; splash-proof
(not immersion); the lid must not fall off when open.

**Interfaces.** (a) lid to box, rotation: close/hinged. (b) lid held shut: close/latches. (c) lid to
box, sealing: close/seal-interface.

**Walk.** `close` asks: open_frequency=daily, tool_free=yes, sealing=splash, hinged_or_removable=
hinged -> `hinged/pin-hinge` (a); `latches/magnetic-catch` (b, tool-free, light hold);
`seal-interface/gasket-and-lip` (c, splash level, printed).

**Rules applied.**
- Pin hinge with a metal pin through printed knuckles: printed moving clearance at least 0.3 mm,
  tuned on a coupon (pin-hinge c1); not a print-in-place hinge, because the daily cycles wear
  printed-on-printed (issue "dry sliding wear grows play").
- Hinge axis near the lid's centre of gravity side, two hinges wide apart (issue "axis too far from
  lid load path"; c14 T = F*e is a shop-rule, guidance only).
- Pin must not walk out: headed, peened or clipped pin (issue "pin walks out under vibration").
- Magnetic catch: shallow node; magnet pull and pocket sizes come from the magnet datasheet
  (principles/look-up-dont-assume); record a dt-gap note for the missing pocket rules.
- Seal: printed TPU gasket or a tongue-and-groove lip. TPU 85A cannot be printed with a 0.4 mm
  nozzle and must be dried (gasket-and-lip c16) -> use a harder TPU or a bought foam cord. Foam
  working compression 25-50% (c2); specify the compressed thickness and a positive stop (issue
  "design by free gasket thickness only"). Do not claim an IP rating from the gasket alone; IP is
  tested on the whole enclosure (issue; c17 unverified, c18 secondary).

**Axis sweep.** locking: n/a, no threads in the closure path (the pin is clipped). captive: relevant,
the lid must not detach -> the clipped pin keeps it; a lid stay is shallow (stay-detent-stop), so
use the hinge's own end stop and record a gap. corrosion: magnet (NdFeB, usually nickel-plated)
glued in PETG, n/a for galvanic; record. bearing-face: n/a. material-direction: relevant, the
knuckle-to-leaf root must not be pulled across layers (pin-hinge issue "sharp root").

**Principles sweep (lid).** DOF: rotation about the hinge axis intended and limited by an end stop
(open) and the box rim (closed); every other DOF stopped by the pin and knuckles in both
directions. Fail-safe: if the magnet lets go, the lid falls open but stays attached (pin clipped):
acceptable. Over-constraint: two hinges on one axis -> the pin must be one straight rod, or one
hinge floats axially. Thermal: PETG lid warms in the sun -> the 0.3 mm hinge clearance is the
minimum, not a target (pin-hinge c11).

**Record.** Three decision notes, `params.leaf` on three connects, principles notes for lid and box,
gap notes: magnetic-catch pocket rules, lid stay.

---

## Notes for builders found while writing these

- `axes/corrosion` carries off-node rules (a2-a4 preload and fatigue, c5 protrusion, c6 preload
  percent, c7 qualification counts, c9 torque accuracy). S1 moves them to `axes/locking` or
  `clearance-tapped` before import.
- ISO 273 rows lack M4 and M5 (the most common maker sizes). S1 adds them from a source.
- The same "start from the printer's own hole test print" rule appears in five rotate nodes; S1
  turns it into one shared rule linked from each.
