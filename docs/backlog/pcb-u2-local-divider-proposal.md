# Local U2 divider placement proposal

The controlled VRDIV-before-VBAT_UV run at e74379865 connects 26/30 signal
nets, gaining VBAT_OV and VRDIV while losing VBAT_UV. Ground and physical
rules remain satisfied. Astra recommends a bounded local placement
proposal next, rather than another routing-order sweep. VBAT_UV currently
spans 74.755480 mm; its R24 also participates in VRDIV. Evidence:
`me567597`, task scratch `astra-u2-order-result-review-1105.md` and the controlled
worker's `order-experiment.json`.

Prepare one offline XY-only proposal for R24 and R25 near U2. Their
original `fixed='rot'` flags permit XY movement; do not change rotations,
sides, pin maps, catalog rows or locks. Hold every other instance exactly
at the accepted full-precision joint placement. Preserve all 159 physical
pads, all 15 authored hard distances, the board boundary, unchanged
0.4 mm courtyard policy, pair gaps and exemptions, and copper/mask rules.

Reuse the existing conservative courtyard MILP and independent canonical
inspector. Use one solve, at most 60 seconds, with both movable part
reservation bounds inside a finite 12 mm square centered on U2's physical
pad centroid (not an assumed footprint-origin frame). Minimize summed
half-perimeter wirelength of VBAT_UV and VRDIV using actual terminal
coordinates and fixed existing orientations; source displacement can
break ties without dominating that objective. Use the source batch's real
memberships, and report them before solving. Keep immutable old tools and
inputs; derive a clearly identified task tool and preserve hashes/model.

After solving, accept only an exact current-source inspection with zero
new placement errors or pair-gap violations, unchanged poses for the other
48 parts, the original flags, original pad identities and 15 raw distance
bounds. Separately report each affected net's before/after span and HPWL.
Do not equate shorter spans with routability.

Use a finite shared-segment-predicate diagnostic for local connections
between R24/R25 and their U2 VBAT_UV/VRDIV endpoints: direct segments and
the two orthogonal one-bend alternatives only. Check foreign pads, holes,
board and explicit zones at the unchanged net widths/clearances. State
whether proposals were tested simultaneously; pairwise legality alone
does not establish a compatible set. Saved routed copper cannot be
reused as a finished route after parts move. A pad-only local witness is
not evidence of compatibility with that copper.

Save the legal candidate, model/placement preview and readback, clearly
labelled as an unrouted proposal. No router, mesh, optimizer beyond the
one bounded MILP, private capture, order matrix, production write or
application deployment is included. A refused/infeasible proposal ends
this probe; report the model's limitations without relaxing constraints.
