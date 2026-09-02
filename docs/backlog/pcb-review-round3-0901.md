---
status: draft
title: "User visual review round 3 (2026-09-01): stitch-via rows, S/N knockout corners, solder nuts, rounded corners, label policy, via relaxation, nano deep-integration"
prio: high
---

# Round-3 review findings + feature asks (user, 2026-09-01)

From peeking at the regenerated `board_nano.svg` / `board_motor.svg`.
Diagnoses verified in-session. **Items 1-7 are DONE** (this worktree,
full pcb sweep 1524 green; deltas noted per item where the implementation
diverged from the first plan), **[filed]** items need their own cycle.

## Diagnosed defects

1. **[DONE] ~40 small vias in rows along the nano board's bottom edge**
   (0.25mm drill, marching through the S/N patch). They are GND plane
   stitching vias (`realize._stitch_plane_fragments` stage-1 sprinkle),
   but `_grid_candidates` enumerates the overlap grid bottom-row-first
   and `max_sprinkle_vias_per_overlap` (24) accepts the FIRST 24 — so
   the whole budget clusters into the lowest rows instead of spreading
   over the overlap. Fix: allocate the budget per disjoint part of the
   overlap (a small island always gets its via) and spread each part's
   share evenly across its candidates. User refinement (2026-09-01):
   ideal placement is inside small islands and ISTHMUSES (necks that
   are only thinly connected on some layer) — the per-part allocation
   covers islands now; narrowness-weighted candidate scoring is the
   follow-on (see item 13's sensitivity work, same cost-shaping pass).
2. **[DONE — inverted] Stitch vias walk under silk furniture** (the S/N
   patch is a *writing surface* — a via bump under the Sharpie area
   defeats it). Shipped the opposite direction from the first plan: the
   furniture placement (title block + S/N patch) is route-independent,
   so instead of grid claims the builders now AVOID vias (soft — scored
   fewest-vias fallback, `silk.via_obstacles`) and part courtyards
   (hard, inflated bbox obstacles in `_board_furniture`), with deeper
   ladders (edge-slide for the title, edge-centre rects for S/N, six
   fiducial rungs) so corner mounting hardware can't evict them.
3. **[DONE — via SVG mask, not ring union] White corners in the "N".**
   `gerber_view._region_els` renders [solid ring + clear rings] as ONE
   `fill-rule="evenodd"` path; where two knockout letter strokes overlap
   (the N's corner joints) the double-count flips the region back to
   filled. Fix: union overlapping clear rings before emitting the path
   (geometry fix in the viewer; real gerber clear polarity is idempotent
   so the artefact exists only in this renderer).
4. **[DONE] `npth_clearance: 4` on the nano board** — the four corner
   mounting holes are never claimed on the routing grid (same family as
   the fiducial copper-claim leak, root-caused 08-31). Fix: claim them
   (hole + annulus + clearance) before `_stamp_pads`.

## Feature asks

5. **[DONE] M4 solder nuts in each corner.** Mounting holes become
   `style: "solder_nut"`: plated hole + copper annulus both sides
   (SMTSO-M4-class: 5.6mm drill, ~8mm ring), rendered + gerber'd +
   DRC'd as plated (pad clearance, not npth), claimed per item 4.
   Corner centres move inboard so the ring clears the edge rule.
6. **[DONE] Rounded board corners.** Outline feature gains optional
   `corner_radius_mm`; the handler polygonizes the rounding at IR build
   (arc facets), so pours/DRC/fiducials/silk all inherit it through the
   one outline polygon. Fixtures set a radius.
7. **[DONE] Label side consistency.** `silk._refdes_candidates` orders
   ring directions "nearest straight up first"; user wants labels on a
   consistent side (right, else bottom) so arrays read tidy. Reorder the
   preference sweep; keep the existing global obstacle avoidance.
8. **[MOSTLY SUBSUMED] Via-out-of-silk relaxation.** A via near C5
   (motor board, also L1/U1) clipped silk ink. Refdes labels already
   avoid vias at candidate time, and the furniture now does too (item
   2), so the remaining exposure is a via clipping a COURTYARD stroke
   (which cannot relocate) — the original idea (post-silk via nudge
   reusing `_shove_vias`) stays filed for that case only. (The "push
   min-gap via pairs apart" half was WITHDRAWN by the user 2026-09-01:
   non-sensitive signals run tight — close vias are fine, see 12b.)
9. **[filed] Placement groups ("super footprint").** J1/J2 are the two
   header rows of a nano daughterboard and must hold their exact
   relative offset; optimize needs rigid group moves (members anneal as
   one body). Generalizes to any repeated-connector cluster.
10. **[filed] Repeated-pattern tiling pressure.** Q1..Q4 / J4-J7 style
    repeated subcircuits should get IDENTICAL internal layout (detect
    isomorphic groups, replicate one layout). Big; interacts with 9.
    The shipped `alignment` term (0.002 USD/pair) is the lightweight
    stand-in already pulling R's into rows.
11. **[filed] Nano netlist deep integration.** Replace the J1/J2
    header pair with the nano's own netlist integrated into the board
    (drop the daughterboard), USB-C connector with an edge-affinity
    constraint ("rubber-banded to the board edge"). Needs: netlist
    source, new fixture, edge-affinity placement term. Own spec when
    picked up.
12b. **[filed] Signal sensitivity classes + tight-bundle routing**
    (user, 2026-09-01). The LLM marks sensitive signals on the netlist
    (none on the current fixtures); sensitive nets get special rules —
    cross power at 90°, GND guard either side, spacing floors. NON-
    sensitive nets are the default and should run TIGHT: parallel
    bundles without extra spacing, vias allowed close together (this
    withdraws the via-island spreading idea). Stitch-via placement
    should eventually prefer isthmuses — thin necks in a plane
    fragment — over open field (narrowness-weighted candidate score).
    Needs: a net-class/sensitivity attribute on the IR, router cost
    shaping per class, and the guard/crossing rules. Own spec when
    picked up.

14. **[filed] Placer is blind to mounting holes.** The ROUTER now claims
    them (item 4) and pours antipad them, but `optimize.py`'s anneal has
    no hole keepout: it can legally park a part's courtyard over a
    corner solder nut, and only the route pass (no_path near the corner)
    or courtyard DRC would complain downstream. Fix: mounting holes as
    static courtyard obstacles in the anneal (same polygon machinery as
    part courtyards). Today's fixtures centre their packs away from the
    corners, so this is latent, not observed.

12. **[filed] Post-route wire shortening.** Wires longer than needed;
    `_collapse_straight` already pulls taut within the corridor, the
    residual slack is maze detours around since-vanished congestion.
    A bounded rip-up-and-reroute pass (re-route each net against the
    final grid, keep if strictly shorter) is the standard fix.

**Verify before delete-on-ship:** regenerate both boards; user re-peeks
(stitch rows spread, S/N writable + clean N, nuts + rounded corners
visible, labels consistent, C5/R1-R4 vias relaxed).
