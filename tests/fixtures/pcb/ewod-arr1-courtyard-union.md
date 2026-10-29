# ewod-arr1-courtyard-union.wkt.gz

The courtyard union input of instance `ARR1` on the hv_ring dogfood
board, dumped during gr536561 (2026-10-29): a WKT `MULTIPOLYGON` of the
54 EWOD electrode pad outlines `instance_courtyard_polygon` unions
before its clearance offset — ~51k vertices total (~800–1400 per
electrode, the crenellated/zigzag mesh edges
`precis.pcb.generators._electrode_polygon` emits).

Produced by putting `tests/test_pcb_island_terminal_polygon.py`'s
`_ring_design()` (8x8 `ewod_pad_array`, pitch 2.25mm, 250V) with
`_ring_footprint(_RING_PINS, per_side=20, x_half=8.5, y_half=11.5)`
cached for `ARR1_SINK_0`, building the IR via `PcbHandler._build_ir`,
and `unary_union`-ing the pad geoms exactly as
`src/precis/pcb/ir.py::instance_courtyard_polygon` does.

Why it exists: a mitre-join `buffer(0.4)` on this input ran ~10–33s per
call (GEOS offset-curve noding is superlinear in input vertices, and the
input's many near-parallel offset edges are the worst case), and a
put+place+DRC cycle calls the courtyard builder ~11 times per instance
set — `tests/test_pcb_declared_pins.py::test_hv_ring_sink_stays_clear_
after_place_job` and `tests/test_pcb_ewod_dogfood.py` read as a ~20
minute hang. Consumer:
`tests/test_pcb_ir.py::test_instance_courtyard_polygon_dense_union_…`.
