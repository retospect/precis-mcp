# pcb view='route' (Freerouting) re-places without the legality gate

`handlers/pcb.py::PcbHandler._render_route`, the demoted Freerouting
escape hatch, re-places with escalating annealing between passes. It does
not run the checks that `op='place'` and `op='move'` now enforce
(`OptimizeEngine.pose_conflicts`, backlog/pcb-always-valid-board-invariant.md),
so it is the one remaining path that can produce an illegal placement.

Decision needed (product, not a bug fix): gate it the same way, or retire
the view now that the in-tree router is the primary path.

test: a `view='route'` pass on a board whose anneal proposes a courtyard
overlap must refuse or legalize, never write the overlapping pose.
Thread: threads/ewod-pcb.md (Horizon).
