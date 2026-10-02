# pcb router: one search can drop two of its own vias too close together

What: `maze.OccupancyGrid.register_via` (2026-10-02) keeps a new via off
every via COMMITTED earlier, of any net. Within ONE search, though, a
path's own vias are not mutually excluded: registration happens at
commit, so a path that goes down and back up within a via's keep-out
can lay two vias whose drills overlap or whose barrels sit under the
fab's trace spacing.

What happens today: nothing silent. The post-route DRC gate
(`pcb_session.routed_drc_findings`) reports `via_via_keepout` ("drilled
holes overlap" / "via barrels clear each other by …"), and
`strip_drc_violating_nets` strips the net. It lands `failed` with note
`drc:via_via_keepout`, so the cost is a lost net, not an illegal board.
It has not been seen on Reto's board (pcb 460559, seeds 1-3) after the
2026-10-02 fixes; it is a known gap in the claim, not a measured
failure.

Fix shape: in the A* expansion, a layer change within the via keep-out of
the path's previous via is impassable. That needs the predecessor chain's
last via position carried in the search state, which the compiled kernel
does not have today. Cheaper alternative: post-search, collapse a
down-up pair closer than the keep-out into a straight run, and re-check
it with `path_is_legal`.

Owner anchor: `src/precis/pcb/maze.py::OccupancyGrid.register_via`. Thread
`pcb-easyeda-round-trip`. test: a fixture whose only route dips one
layer under a 1-cell obstacle must produce vias ≥ keep-out apart, or
fail the segment.
