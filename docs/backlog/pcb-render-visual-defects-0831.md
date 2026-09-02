---
status: draft
title: "User visual review 2026-08-31: silk off-board / no fills / fill margins / 90-45 routing / silk-on-mask-only"
prio: high
---

# User visual review 2026-08-31 — six findings from looking at the boards

Reported by the user peeking at `board_motor.svg`, `board_nano.svg`
(handler id `renderfixture`), `board_seed1/2.svg` in the worktree root
(regenerate: `tests/test_pcb_render_fixture.py` env-gated utility, or the
fab-render test; view via `qlmanage -t -s 1800 -o /tmp/pcbpeek *.svg`).

## Status after the 2026-08-31 fix session (this worktree, unshipped)

1. **Silk outside the board outline — FIXED.** `build_silk` takes
   `outline=`; refdes candidates and pin-1 dots are rejected outside it
   (`_box_inside_outline` / `_circle_inside_polygon`), and DRC's
   `check_outline_containment` now also flags silk ink off-board.
2. **Silk-to-board-edge margin — FIXED.** `_silk_edge_margin_mm`
   (capability `board_edge_clearance_vcut_mm`, house→jlc→0.6 fallback)
   enforced at generation + new DRC `check_silk_edge_clearance`
   (two-tier, same field as copper's edge rule).
3. **Silk on soldermask only — COVERED by 1+2 plus the pre-existing
   mask-opening clearance** (`_mask_openings` — silk already clears
   pad/paste mask windows, vias are tented). Residual cosmetic gap: the
   SVG renderer still draws no mask film layer (gerber_view does).
4. **Plane fills — FIXED.** Fixture JSON `"planes"` key honored by the
   render utility; `nano_oc_switch.json` and
   `motor_power_reference.json` pour GND on both outer layers.
5. **Fill margins / corner clustering — FIXED (two parts).** Placement:
   `recentre_in_outline` rigidly translates the FINISHED placement to
   the outline centre (post-anneal — a centred *seed* flipped reference
   seeds to no_path; gated off for placeholder canvases by
   `_RECENTRE_MAX_AREA_RATIO`). Pour clip inset was already the shared
   `edge_inset` figure and stands.
6. **Fully 90/45 routing — FIXED + ENFORCED.** All arbitrary-angle
   emitters on the maze path re-worked octilinear (`_collapse_straight`
   taut pull with elbows, `_octilinear_shove_target`, quantised
   `_DROP_SEARCH_OFFSETS` fan-out stubs); new DRC rule
   `check_octilinear` (error severity) folds into the acceptance
   fixtures' copper-class hard zero on all seeds. Fillet arcs between
   octilinear runs are deliberately kept; the tangent router (not on the
   job path) is out of scope.

Bonus root-cause closed en route: render-time FIDUCIALS were never
claimed on the routing grid → the 0.000mm clearance / via_pad_keepout
family (`pcb-plane-via-copper-claim-leak.md`). Router now pre-claims the
candidate-corner superset (`silk.fiducial_candidate_sites`, thicket
bbox excluded on both mint and claim sides).

## Second review round (2026-09-01), all addressed

- **Inner-layer fills**: nano fixture pours In1.Cu=GND, In2.Cu=VBAT
  (8A rail) — all four layers filled.
- **Fill to the edge**: pour rim now insets by the board-edge rule only
  (was inheriting the track-centerline figure incl. half the widest
  track — realize's `_pour_planes` call site comment). D3/R3/J7 now
  inside the fill.
- **Superfluous drop vias** (R2 on the motor board):
  `realize._prune_redundant_drop_vias` — pad covered by its own net's
  same-layer pour needs no dogbone; stitcher re-adds only genuine
  inter-layer joins.
- **Fiducials**: netless by design (optical targets); the thicket
  filter became per-courtyard (`silk.world_courtyard_rings`) after the
  bbox form yielded a zero-fiducial nano board.
- **Alignment bonus**: new summed-family `alignment` cost term
  (north-star invariant 3's first aesthetic pressure), tuned 0.01→0.002
  the same day when the stronger value steered a reference seed
  unroutable.
- **Voronoi tiling status** (user asked): `pcb/tiling.py::grow_tiles`
  (Slice 5) is built + unit-tested but never wired into realize — pours
  come from `planes.plane_pours`. Wiring the tiling in as the copper
  realizer remains the open integration (tracked in
  `pcb-guided-place-route.md` Slice 5).

Open residue for a future pass: (a) SVG mask-film layer (item 3
cosmetic); (b) a spread-to-fill aesthetic cost term if sparse authored
boards should ever *use* their extra area (north-star file).

**Verify before delete-on-ship:** user re-peeks regenerated
`board_motor.svg` / `board_nano.svg`.
