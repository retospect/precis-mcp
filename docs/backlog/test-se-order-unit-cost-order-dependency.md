# test_se_order fails when `unit_cost` was auto-minted under another category in the same xdist worker

Bug (found 2026-09-19 by a hybrid local gate on integrated main; does not
reproduce on GitHub, where sharding separates the files). Three
`tests/test_se_order.py` tests red with `BadInput: spec='unit_cost' applies
to category 'bearing', not 'fastener'|'widget'` — i.e. in that worker's DB
clone the 0093 universal core spec `unit_cost` was absent and
`tests/test_se_bom.py` (`bearing-608` at ~L824) auto-minted it as a
bearing-scoped *proposed* spec, which `component_specs` (preserved across
tests, "core + proposed") then carried into every later test. The
template itself had `unit_cost` universal afterwards, so the loss is
per-clone and mid-run; mechanism not yet identified (`_ensure_component_seed`
runs only at template maintenance, and its guard probes category-scoped
core specs, so a missing *universal* 0093 row is invisible to it).

Owner anchor: `tests/conftest.py::_ensure_component_seed`,
`src/precis/handlers/component.py::_mint_spec` ("never universal").

Test: run `tests/test_se_bom.py tests/test_se_order.py` in one worker
(`-n 0`, se_bom first) against a clone whose `unit_cost` row was deleted —
must stay green; then find who deletes it.
