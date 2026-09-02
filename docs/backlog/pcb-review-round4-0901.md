---
status: draft
title: "User visual review round 4 (2026-09-01): bottom-side half-support, mask viewer, all-layer fiducials, pin-1 polarity, courtyard via clips, placer holes, rigid groups + pattern tiling, hierarchical titles"
prio: high
---

# Round-4 review findings + feature asks (user, 2026-09-01)

From peeking at the round-3 regenerated `board_motor.svg` /
`board_nano.svg`. Diagnoses verified in-session before dispatch.

## Motor board

1. **[DONE — fixture → top; full support filed as item 10] "C3 on the
   bottom, why?"** — `motor_power_reference.json` declares C3
   `"layer": "bottom"` (added with the second reference board,
   74390332, to exercise bottom silk). Diagnosis: the component-side
   declaration is HALF-honoured — the handler's refdes→layer map feeds
   only `silk.build_model_silkscreen` (mirrored bottom silk); pads,
   mask, paste and routing all still emit on F_Cu ("F_Cu · pad · pin
   C3.2" in the very same SVG). A bottom part today is a silk-only lie
   in the gerbers. Fix now: C3 back to top. Full bottom-side support =
   item 10.
2. **[DONE — router-side ink field] Vias near U1/C5 still clip silk** (nano: D4 too). Labels
   already avoid vias; the residual clips are COURTYARD strokes, which
   cannot relocate. Root fix at the source: courtyard-stroke rings are
   placement-derived and known BEFORE routing — add a soft via-site
   penalty (not hard block) under the future courtyard ink
   (`world_courtyard_rings` + silk clearance) so the router stops
   dropping vias there. Replaces the filed post-silk nudge idea.
3. **[DONE] Fiducials span all layers.** Currently flashed on
   `layers[0]` only. Emit the copper disc on EVERY copper layer, mask
   opening on BOTH sides, pour antipads on all layers, router keepout
   claims on all layers. Silkscreen stays deliberately EMPTY at the
   fiducial (a silk-free zone IS the alignment feature; ink there
   would defeat the optical target).
4. **[DONE] Pin-1 marks only where polarity exists.** R/C/L/FB refdes
   families get NO pin-1 dot unless polarized: explicit
   `"polarized": true` on the component, or label matching
   ELEC/TANT/POL. Nano C1 (CAP-ELEC…) is the live test case. All other
   families (D, Q, U, J, LED…) keep the mark.
5. **[DONE — film render] "Solder mask layers are empty."** Two findings: (a) B_Mask
   had only nut rings because the ONLY bottom part is the C3 half-lie
   (item 1) — correct once C3 is top; (b) the real UX bug: mask
   gerbers contain openings that sit exactly on the pads, so toggling
   the layer shows nothing new. Render mask layers in the viewer as
   what they ARE physically: a translucent film over the whole board
   with the openings cut out (reuse the SVG `<mask>` machinery from
   round 3).

## Nano board

6. **[DONE] J1/J2 rigid group ("super footprint").** Components gain
   `"group": "<name>"` (+ per-member `group_offset {x, y, rot}` fixing
   internal geometry — J1/J2 at the nano's real 15.24 mm row pitch).
   The anneal moves a group as ONE rigid body: translate/rotate apply
   to all members about the group centroid, internal offsets locked,
   legality checked per member. Fits the existing multi-instance
   `Move` shape (SWAP already carries 2 instances).
7. **[DONE — v1, rigid tiles stamped from instance 0] Repeated-pattern tiling.** Components gain
   `"pattern": "<name>"` + `"pattern_instance": <n>`. All instances of
   a pattern share ONE internal layout (leader = instance 0; followers
   stamp the leader's internal member offsets at seed) and anneal as
   rigid bodies — identical tiles by construction, the alignment term
   pulls them into rows. Nano: channels {J4,Q1,R1,D1} … {J7,Q4,R4,D4}.
   V2 (filed): co-optimize the shared internal layout mid-anneal;
   automatic isomorphic-subcircuit detection.
8. **[DONE] Placer blind to mounting holes — now OBSERVED** (Q3 overlaps
   the top-left nut, R1 possibly fully under it, C1 clipping). Round-3
   item 14 promoted: mounting holes (ring dia + courtyard clearance)
   become static courtyard obstacles in the anneal
   (`_placement_is_legal` + overlap term), read off
   `ir.mounting_holes`.

## Viewer

9. **[DONE] Hierarchical mouseover.** Titles lead with the layer, then
   position, then what the element belongs to: "F_Cu · (x, y) mm ·
   pad 1 of Q4 · net OUT4"; tracks/regions carry their net; keep the
   existing escape rules. Viewer-only formatting + ownership plumb.

## Filed

10. **[filed] Full bottom-side component support.** `"layer":
    "bottom"` must flow past silk into `pads_for_ir` (mirrored B_Cu
    pads), mask/paste sides, router pad-layer starts, and a placer
    SIDE_FLIP-for-instances move. Until then the loader should warn on
    (or reject) bottom parts rather than half-honour them.
11. **[filed] Tiling v2** — mid-anneal shared-layout co-optimization;
    automatic isomorphic-group detection (round-3 item 10's full
    scope).

**Verify before delete-on-ship:** regenerate both boards; user
re-peeks (C3 top, no courtyard via clips, fiducials on every copper
layer + both masks, no pin-1 dots on R/C except C1, mask film
renders, J1/J2 locked, channels tiled identically, nothing under the
nuts, hierarchical tooltips).
