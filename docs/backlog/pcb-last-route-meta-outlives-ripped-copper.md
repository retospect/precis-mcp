# pcb meta.last_route still reports a route after every net was ripped

Reto on heater-base-test (2026-10-02): "I don't see any vias or drills and
no routing."

- Route job 460712 (01:50Z) realized 66 nets, failed 23, and placed 399
  vias.
- From 01:55:13 to 01:58:53Z, 89 single-net `put`s (profile `typed`, no
  agentlog) set every `pcb_routes` row to `unrouted`, and `pcb_copper` now
  has 0 rows. It is not known who ran them.
- `meta.last_route` still reports the 66-realized result, so the board
  claims a route it no longer has.
- The source's 1579 tracks and 234 vias are deliberately not imported
  (Reto's 2026-09-30 ruling: measure them, don't import them;
  `ingest/pcb_epro.py` docstring).
- Drills are there: `view='gerber'` lists 126, the Ø6 mounting holes plus
  the through-hole pads. Not checked: whether the fab SVG draws them where
  they can be seen.

Fix direction: clear or flag `meta.last_route` when stored copper
contradicts it, and have the route summary report the copper actually
stored. Whether to re-route the board is Reto's call.

test: route a board, rip every net via put, then the bare get and
view='route' report 0 realized and show no stale last_route.
Thread: threads/pcb-easyeda-round-trip.md.
