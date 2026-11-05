---
status: ready
---
# PCB viewer pending-net connectivity (gr565255, td565473)

Scope `src/precis/pcb/connectivity.py::pin_components` to pending nets before
extracting primitives, preserving default behavior and each selected net's
component partition. Pass the pending set from
`src/precis/pcb/gerber_view.py::render_fab_svg` so unrelated routed segments
do not enter the quadratic connectivity pass.

Include solid, pin-attributed copper regions in
`src/precis/pcb/gerber_view.py::_ratsnest_els` before component contraction.
Use the same pin identity as flash pads and connectivity; polygon orphan
pads must keep their airwires. Keep all Gerber writers unchanged.

Add regressions for excluded routed segments, unchanged pending-net
components, and the polygon C3.2 orphan in the partly connected fixture.
Tests and browser verification belong to the coordinator; this tree runs
Ruff only and adds one commit for review. Delete this spec on landing.
