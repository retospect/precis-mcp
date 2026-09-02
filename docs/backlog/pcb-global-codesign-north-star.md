---
status: idea
title: "North star: global generative co-design (the dragon board) — invariants, not a feature"
prio: low
---

# North star: global generative co-design (the dragon board)

Stated by the user 2026-08-31. Not planned work — a standing constraint on
how the place/route engine grows, so nearer-term slices don't paint it
into a corner.

**The goal, by example.** "Make a PCB with function X that generally looks
like a dragon": two square chips as eyes, oriented to read as eyes;
prevalent trace direction as fur; resistors/capacitors aligned with scale
direction; M4 mounting screws as claws, attachment points shifted so both
the image and the injection-moldable enclosure work; CFD steering hot
parts into airflow. Local optimizers (place, route, silk) plus global
interaction pressures, all gradual, all co-descended. Billions of feasible
solutions exist; the job is to find ONE OK solution, not the optimum.

## What the current architecture already gets right (keep it)

- ONE cost function of pluggable, justified terms (`cost.TERMS`), summed
  money + max-aggregated margin, one risk↔money dial. New global
  pressures are just more terms; nothing needs restructuring.
- Hard/soft separation: `_placement_is_legal` is exact and categorical;
  cost terms only steer. Aesthetic pressures are *soft by nature* and slot
  into the steering side without touching legality.
- Rotation is a first-class continuous variable; courtyards are true
  per-part polygons, not radii. A direction-field term ("align passives
  with the scale direction here") needs exactly this.
- Multi-seed acceptance (fixtures measure the engine's distribution, not
  one draw). "Billions of solutions, need one OK one" makes best-of-N /
  worst-of-N the only meaningful acceptance shape.

## Invariants to hold as terms accumulate

1. **Two classes of soft term, and they have different contracts.**
   *Constraint relaxations* (courtyard spacing, edge clearance) must share
   their hard rule's geometry and cross "at budget" (fraction 1.0) exactly
   at the legality boundary — two geometries for one question is the drift
   the courtyard work spent two ships removing. *Preference fields*
   (fur direction, eye placement, thermal airflow) have no hard
   counterpart and are free-form; they must still be move-reachable and
   justified (`TermSpec.justification` is already required).
2. **Don't let convex-SAT assumptions leak into board-outline handling.**
   A dragon outline is deeply concave. Pairwise courtyard overlap stays
   convex-SAT (parts are convex hulls); outline containment must use
   concave-safe primitives (`point_in_polygon` ray-cast is already fine).
   `outline_bbox` approximations are documented as rectangular-board
   proxies — each new consumer must re-justify, not inherit.
3. **The margin MAX aggregate is a bottleneck for many weak pressures.**
   Aesthetic terms are numerous-and-mild; an exact max only ever feels the
   single worst entry board-wide. `p_norm` exists in `cost.aggregate_
   margin` but `OptimizeConfig` forbids it (not per-move decomposable).
   When preference fields arrive, they likely need their own SUMMED
   family (like money) or a decomposable soft aggregate — decide then,
   but don't build new terms that secretly assume max-aggregation.
4. **External couplers (enclosure moldability, CFD) enter as cost terms
   over shared anchors** (mounting points, hot-part positions, board
   outline control points), not as a second optimizer that owns the
   board. One anneal, many pressures.

## Explicitly NOT in scope now

Everything above the invariants: no image-generation coupling, no
direction-field term, no enclosure/CFD integration, no concave courtyard
support. This file exists so those, when asked for, are additive.
