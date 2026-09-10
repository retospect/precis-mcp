---
status: draft
title: "pcb: congestion-driven spread — place↔route iteration with a spatial congestion map"
prio: high
model: opus
---

# pcb congestion-driven spread

Design session 2026-09-10 (Reto + agent), zippy-booping-sky worktree.
Reto: "shove things apart and iterate back and forth a few times, make
a congestion map maybe."

## Problem (measured, deterministic)

Correcting the esp32c3 reference netlist (8 previously-dangling
connections wired — commit `1010bbd`) makes place+route fail the
hard-zero copper-DRC acceptance at 4/5 seeds (2–6
`connectivity`/`unrouted` errors, one new silk drop): the placer
packs all parts into ~32 mm regardless of the declared 300×300 mm
outline, and the corrected board's extra routing demand no longer
fits the self-inflicted clump. The old zero was measured against the
broken (easier) netlist — the engine has never routed the true board
clean. `tests/test_pcb_reference_end_to_end.py` holds the per-seed
breakdown; the packing gap was already named in that fixture's
docstring.

Root cause shape: **nothing in the placement cost ever pushes apart.**
Wirelength and proximity measures pull together; courtyard legality
is the only repulsion, and it stops at touching. Congestion knowledge
exists only as a post-route warning digest
(`realize.py::CongestionWarning`, rendered by
`PcbHandler._render_congestion` from the latest `op='route'` run) —
never fed back into placement.

## Proposal — three parts, in dependency order

1. **Spatial congestion map.** Rasterize the outline into cells
   (~2 mm). After a route run, score each cell from evidence the
   engine already produces: realized copper occupancy per layer,
   `CongestionWarning` gaps, via density, and for each FAILED net a
   corridor along its ratsnest line (failure pressure must land
   where the route wanted to go, not nowhere). Persist per run like
   DRC findings ("no map yet" ≠ "no congestion"). Agent-facing:
   extend `view='congestion'` with the map (text heat summary; SVG
   overlay can join the existing renderer later).
2. **Spread pressure in placement.** Two cost terms in the anneal
   (`optimize.py`), both computed from cheap grids:
   - *density prior* (route-independent): per-cell courtyard-area
     density above a threshold costs linearly — stops the 32 mm
     clump even on round 1, uses the outline the placer currently
     ignores. NOT "fill the board": below-threshold density is free,
     so wirelength still compacts locally.
   - *congestion pressure* (route-fed): instances overlapping hot
     cells of the round's congestion map pay per overlap·heat; a
     dedicated coarse move class ("shove apart": displace along the
     local heat gradient) so the term is reachable, not
     measure-zero — the discrete-vs-continuous lesson from this
     build.
3. **Place↔route iteration.** `op='route'` grows a bounded loop
   (default 2–3 rounds): place → route → map → re-place seeded from
   the current placement with congestion pressure on → re-route.
   Stop early on DRC-clean or no strict improvement (compare
   (unrouted, DRC-error) tuples). Each round's map + outcome
   persists — the iteration is inspectable afterwards, per-round.

## Guardrails (lessons already paid for)

- Single source for every rule: cell grid + density math live in ONE
  module consumed by cost, map, and view (the two-sites-drift
  generator).
- Sweep seeds 1–5 before trusting any constant; results are working
  RANGES (threshold, cell size, round cap), not optima. The fixture
  memory: a red fixture invites nudging constants to green — don't.
- A map that cannot fire (no route run yet) must say so in its
  output; never render empty as cool.
- The acceptance criterion is the corrected esp32c3 fixture
  hard-zero at ALL 5 seeds (restoring the "never again" invariant on
  the true netlist), motor board stays clean, runtime within ~2× of
  a single place+route.

## Fixture correction reverted at land (reapply here)

The corrected fixtures ("fix(pcb): wire dangling R/C pads + header
breakout in reference fixtures" — esp32c3 + motor_power fixture JSONs
plus expectation updates in the end-to-end / fab-render / second-design
tests) were reverted before landing this branch: on the true netlist the
current router leaves 1 unrouted + 1 connectivity copper error at every
seed, which breaks the never-regress gate. Reapplying that correction is
part of this item's acceptance criterion above — redo the wiring (or
recover the reverted commit from this branch's pre-squash history) once
the spread pass exists, and the fixtures must then route hard-zero.

## Explicitly not in scope

Global routing / rip-up-and-reroute redesign; SVG heat overlay
polish; exposing iteration knobs to agents beyond a round cap.
