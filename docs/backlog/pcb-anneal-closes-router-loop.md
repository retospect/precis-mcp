---
status: ready
title: PCB anneal closes the router loop
pillar: 3d-design
prio: high
model: opus
---

# PCB anneal closes the router loop

## Motivation / why

Reto, via orchestrator 2026-10-06 21:58Z: the anneal, not an LLM, should
correct placement when routing fails. This supersedes the Nano brief's
placement-advice step. Routing evidence must change the next anneal's
objective; merely rerunning an unchanged placer is not feedback. The LLM
and Reto receive the final constraint-conflict digest, not placement advice
or intermediate retry instructions. This item is ready as a specification,
not authorised for immediate implementation or a claimed working feature.

“As big as it needs to be” becomes a priced, authored-bounded design move.
The acceptance board is the synthetic
[USB-C PD Nano test board](pcb-usb-c-pd-nano-testboard.md), with fixed PDO
rails 5/9/15/20 V and its other design corrections applied before layout.
Nano compatibility describes the headers, not a mandatory 18 × 45 mm outline.

## In scope

1. Extend `src/precis/pcb/optimize.py::MoveKind` with **OUTLINE_GROW**
   (edge plus a positive millimetre delta) and **LAYER_ADD** (a supported,
   symmetric copper layer pair). Price outline growth using the fab-table
   area cost in the money family, and a layer pair using its fab-table USD
   cost delta at the same authored quantity/process basis. Do not substitute
   a free shape change or a synthetic tuning weight for fabrication money.
   Required authored caps: maximum outline dimensions in mm, maximum copper
   layers, and maximum estimated fab USD on that explicit cost basis.
   Missing caps do not mean infinity; disable/refuse the priced expansion.
   Missing fab pricing returns an explicit unavailable-price constraint,
   never zero USD. Existing budgets and fixed design geometry stay binding.

2. Feed each router outcome into the next anneal's cost. A failed net adds
   a hard margin penalty to its implicated region cells; a failed escape
   adds a pad-neighbourhood penalty tied to that actual terminal. Feed
   PathFinder's final present-congestion per cell into region demand, with
   layer/grid-to-board-mm provenance. Aggregate to `RegionEntry` rather
   than guessing the failed net's region from its name or treating an
   unknown cause as congestion. Keep routing-demand units separate from
   USD; expose the conversion/normalisation used by the margin cost.
   Preserve the router's failure and unknown-cause evidence.

3. Run a budgeted outer loop: route → penalise → anneal → route, with two
   rounds by default and an authored maximum of three. Use the complete
   legal move set, including translate/rotate/swap/side-flip/layer-assign/
   plane-promote/plane-demote/pin-swap and the two priced additions.
   Existing wire-topology side flips are not physical component-side
   changes: do not reinterpret them to evade fixed-part or assembly rules.
   Maintain the always-valid-board invariant on every accepted state and
   round; pad/copper legality, per-net widths/clearances, planes, stackup
   symmetry and fixed/terminal claims remain authoritative. Reject an
   illegal move and restore the complete prior state/cost, including
   outline/layers/copper. An incomplete legal route is not success.
   Round, iteration, time and memory budgets are hard bounds, not excuses
   for provider calls or an unbounded retry loop.

4. The only new LLM-facing result is the end-of-loop digest: binding
   constraint conflicts, net/pad/region witnesses and shadow prices or
   finite marginal costs with units and the fabrication-cost basis.
   Example shape: “3 nets unroutable with J1 pinned north and outline
   ≤60 mm; freeing either clears them,” with the corresponding margin
   terms and cost values. Claims that relaxing a constraint clears nets
   require a bounded replay/counterfactual witness; otherwise label the
   effect unknown, not an inferred fix. Internal rounds/moves belong in
   retained evidence, not an LLM placement-advice workflow. Unknown prices
   and exhausted budgets appear explicitly in the digest.

## Explicitly NOT in scope

LLM-directed placement, repeated unchanged route jobs, provider/catalog
refreshes, JLC ordering/manufacture, real-board edits, silent router/DRC
threshold relaxation, or changes to scientific/service workloads. This
filing does not implement A/C or reopen EasyEDA round-trip. Fabrication
costs are estimates, not purchase quotes. Unsupported layer stacks are
not made valid by a low cost; layer-pair moves must reach the actual
hydration/router/DRC/export consumers before being accepted.

## Acceptance criteria

- A deterministic synthetic Nano PD fixture deliberately congests its
  power nets at an authored capped outline. With a priced permitted
  expansion, the loop grows the outline or adds a supported layer pair
  and routes those power nets cleanly within two/three rounds. Record
  seed, full rules, before/after dimensions/layers, routed/failed/dangling
  counts, cost basis/delta and complete DRC evidence; repeat the seeded
  case to verify identical state and results.
- The same fixture with caps forbidding both expansions returns a
  conflict digest naming the binding outline/layer/USD/fixed constraints
  and implicated nets/regions. It does not claim routed success, clear
  losses silently, or offer placement advice. The fixture must actually
  isolate the capped conflict; this result is not yet measured.
- Failed-net, failed-escape and present-congestion evidence each reaches
  its correct region demand/penalty. Missing witnesses remain unknown.
  Legal rotation/swaps and existing electrical moves remain eligible;
  no declaration that an enum member alone guarantees useful motion.
- Expansion-cap, unavailable-price, illegal-stack and invalid-board
  controls refuse atomically. No accepted state violates fixed design
  geometry, fab limits, authored rules or time/memory/round bounds.
- Digest marginal values have stated units, finite-difference or witness
  provenance, and distinguish estimated prices from measured routing.
  “Freeing either clears them” is asserted only for witnessed controls.

## Target + blast radius

Current source premise checked at main
`1c412f327584a2e81df0c81d4a5bd9f07022dc07`; native Python discovery was
tried first, then symbol get. Served/indexed `/src` was the earlier R16
root, not this isolated main-based task tree, so bounded local excerpts
verified these durable anchors:

- `src/precis/pcb/optimize.py::MoveKind`, `MOVE_GENERATORS`,
  `OptimizeEngine.money`, `OptimizeEngine.digest`, `RegionEntry`.
- Requested **TermEntry** seam is currently named
  `src/precis/pcb/optimize.py::TermSummary`; `Digest.terms` carries it.
  Cite the actual symbol rather than inventing an existing TermEntry API.
- `src/precis/pcb/maze.py::Negotiation`: `usage`, `pres_fac`, history and
  present-congestion search surcharge; negotiated congestion is a
  per-route opt-in today, not a shipped outer-loop feedback channel.
- Existing router worker/session, outline/stackup hydration, fab pricing,
  geometry/DRC and atomic persistence are consumers of the proposed moves.
  Reuse those seams; no new worker→handler imports or schema assumption.

## Open questions / decisions log

- **Decided (Reto 21:58Z):** anneal owns placement correction, priced
  outline/layer changes and bounded route-feedback rounds. End digest
  carries constraints/shadow prices, not instructions to an LLM.
- **Decided (Reto 21:35Z):** grow the Nano test board as needed; author and
  record its routing/clearance-driven outline. Expansion here is priced
  and bounded, not a claim that the initial outline must be tiny.
- **Docs fold approved (Reto, 2026-10-07):** publish this spec and Nano
  readiness on main. Routing prerequisites proceed as distance assignment,
  driver rotation, then lane template, on snapshot replay only. This fold
  does not implement the outer feedback loop or authorize real-board jobs.
