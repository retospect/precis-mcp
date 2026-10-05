# ewod-dogfood-6 replay v1

Deterministic gzip-compressed versioned JSON, captured 2026-10-05 with
scripts/pcb-snapshot export-prod through prod-psql --ro (agent_ro; SELECT only).
Reto explicitly confirmed these dogfood board data are public. Raw polygons,
footprint pads/dimensions, canonical generator recipe, historical/active swaps,
fixed/derived copper and routing checkpoints are preserved, not regenerated.
IDs are fixture-local; non-routing timestamps are omitted. No credentials,
unrelated refs, provider request or executable job is included.

Source engine at capture: 8b055c1bd41cc1b17a2d1f021a336c42a9e94ab1.
This does not identify the historical engine that produced the stored copper.
Latest persisted route params omit seed/iters/negotiate; captured current worker
configuration resolves seed 0, iters 3000, negotiate 0. Dynamic constraints reside
in the relational state; OptimizeConfig defaults are not a resolved anneal state.

2 instances /58 nets /55 escape connections; before reroute the existing
route-status reader must return 22 routed /33 failed /3 dangling. 17 no_path,
16 congestion, zero over-capacity gaps. Loader only creates a fresh dev/test slug;
never replaces a design/cache. Re-export explicitly carries returned route params
because no job is cloned. Optional PRECIS_PCB_REPLAY_FIXTURE selects another JSON
or .json.gz capture. Tests use the checked-in fixture by default.

Run: scripts/test -n0 tests/test_pcb_snapshot.py
Internal script: scripts/pcb-snapshot load-test <fixture> <new-slug>
requires PRECIS_TEST_PG_URL; database-name guard also refuses production targets.
