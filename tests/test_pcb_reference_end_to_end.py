"""ESP32-C3 acceptance vehicle — end-to-end place + stored-DRC ratchet.

``docs/backlog/pcb-guided-place-route.md``'s acceptance vehicle
(``tests/fixtures/pcb/esp32c3_reference.json``, structurally pinned
DB-free by ``tests/test_pcb_reference_design.py``) driven for real here:
author the design, ``put(op='place')`` -> drain the enqueued ``pcb_place``
job, then read back the (retired) route state and ``view='drc'``'s answer.

``put(op='route')`` is retired (gr515107, Reto 2026-10-25: the vector
router was deleted, tag ``vroute-final``; topo route rides
``op='place'`` with ``placer='topo'``, ``route=True`` — this test drains
a place-only job, so no route runs). The ``put(op='route')`` leg and
the routed-count assertion died with it — every fanout>=2 net now reads
``unrouted`` and that is the expected state, not a failure. What remains
gates placement and stored DRC exactly as before; the measurements below
are the dated record of how the board got here.

2026-08-28 first-run measurement (commit ``4ff7add4`` plus the — at the
time of that run — still-uncommitted ``core.put`` ``args=`` fix that made
``put(kind='pcb', args={...})`` reachable over MCP at all, see
``git diff -- src/precis/tools/core.py``; the handler-level path this test
uses was already reachable): **81.8% routed** (9 of the 11 nets with
fanout >= 2 realized), **1063 DRC errors** — ``clearance`` 1038,
``board_edge_clearance`` 20, ``courtyard_overlap`` 5 — from 54 vias / 61
tracks of realized copper. GND and VCC3V3 (the two highest-fanout nets)
FAILED to route, despite neither being plane-promoted.

**2026-08-28 late: the acceptance criterion is now MET and asserted.**
Zero DRC errors, all 11 fanout>=2 nets realized. (Dated record: the silk
rules did not exist yet. Copper still holds at zero; silk carries its own
ratchet, ``BASELINE_SILK_ERRORS_BY_SEED``.) Three changes got it there, in this
order of contribution (the routed half is the dated record — see the
retirement note above):

1. Per-pin pad geometry (``ir.pin_dx``/``pin_dy``). Every pin used to
   resolve to its instance's centroid, so a 14-pin part emitted 14 tracks
   on 14 nets all starting at one coordinate — ~600 clearance errors at
   an exact 0.000mm gap that no router could ever fix. 1063 -> 612.
2. Hard placement constraints (``OptimizeEngine._placement_is_legal`` /
   ``bounds_for``), replacing two graded cost terms that the search was
   simply paying: parts may not overlap and may not hang off the board,
   with both keep-outs derived from each part's OWN land pattern rather
   than a single 1.0mm constant. 612 -> 234, and courtyard_overlap and
   board_edge_clearance both to zero.
3. The vector router (``precis.pcb.vroute``).
   Copper is claimed on a shared occupancy grid before it is drawn, so
   inter-net overlap is structurally impossible. 234 -> 0.

**Zero DRC is trivially achievable by routing nothing, so this test used
to assert BOTH numbers and neither alone was the result.** An early
revision of the maze router scored a perfect zero while leaving 58 of 61
connections unrouted, and read as a triumph until the routed count was
put beside it. The copper-DRC ceiling was a hard 0 rather than a ratchet
because it was a property of the occupancy-grid algorithm, not a tuning
outcome — the entire guarantee died with the router, and on this
all-synthesized board ``view='drc'`` now answers the retirement body
instead of a findings table (there is no real pad geometry to check).
The seed discipline stays: every seed still runs the place leg, and the
unrouted/retirement assertions hold per seed (``SEEDS``), so a bound
here describes the engine rather than one draw of the anneal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from precis.workers.executors._common import TERMINAL, current_status
from precis.workers.executors.job_inproc import run_job_inproc_pass

# Heavy compute (a full simulated-annealing place + route pass over a
# 29-component / 20-net / 81-connection design) — see
# docs/conventions/testing.md § "Judging effectiveness".
pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).parent / "fixtures" / "pcb" / "esp32c3_reference.json"

#: The seeds this fixture asserts over — EVERY seed must hold every bound
#: below (worst-case-over-seeds, not a median), so the baselines measure
#: the ENGINE rather than one lottery draw of the anneal. A single pinned
#: seed was the root cause of the constant-nudging pressure recorded in
#: the courtyard-polygon work ("the fixtures pin one
#: lottery draw"): any placement-affecting change re-rolled the draw, a
#: correctness fix could read as a regression, and the same clearance
#: sweep ranked its four candidate values differently before and after an
#: unrelated legality gate. Five seeds is not a distribution either, but
#: it is enough that a bound holding across all of them is a property of
#: the search and not of seed 1. Each seed runs as its own parametrized
#: test (~16s, xdist-parallel), so a failing seed names itself.
SEEDS: tuple[int, ...] = (1, 2, 3, 4, 5)


def _fanout(connections: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for conn in connections:
        counts[conn["net"]] = counts.get(conn["net"], 0) + 1
    return counts


def _drain_one_job(store: Store, parent_id: int) -> None:
    """Drain the queued job most recently enqueued under ``parent_id``.

    Not just "whatever's next in the queue" (gr295496): the shared
    per-worker test DB can carry an orphaned job left behind by an
    unrelated test file, and ``run_job_inproc_pass(limit=1)`` claims
    strict priority/age order, so a stray row — not this design's own
    job — gets claimed first and its failure misreads as OUR drain
    failing. Loop passes, tolerating whatever unrelated row lands along
    the way, until THIS design's own job reaches a terminal status.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
    assert row is not None, f"no job was ever queued for parent_id={parent_id}"
    job_ref_id = row[0]

    for _ in range(25):
        with store.pool.connection() as conn:
            status = current_status(conn, job_ref_id)
        if status in TERMINAL:
            with store.pool.connection() as conn:
                meta_row = conn.execute(
                    "SELECT meta FROM refs WHERE ref_id=%s", (job_ref_id,)
                ).fetchone()
            assert meta_row is not None, f"job {job_ref_id} disappeared"
            job_meta = meta_row[0]
            evidence = {
                "job_id": job_ref_id,
                "status": status,
                "meta": job_meta,
                "routes": store.pcb_routes_get(parent_id),
            }
            path = Path(".scratch/r14-route-gate")
            path.mkdir(parents=True, exist_ok=True)
            seed = job_meta["params"].get("seed", 0)
            job_type = job_meta["job_type"]
            (path / f"reference-{job_type}-seed{seed}.json").write_text(
                json.dumps(evidence, indent=2, default=str), encoding="utf-8"
            )
            assert status == "succeeded", (
                f"job {job_ref_id} failed to drain cleanly: status={status!r}; "
                f"error={job_meta.get('error')}"
            )
            return
        result = run_job_inproc_pass(store, limit=1)
        assert result["claimed"] == 1, f"expected a queued job, got {result}"
    raise AssertionError(f"job {job_ref_id} never reached a terminal status")


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.timeout(600)  # CI junit worst 136s x4 for CI variance (gr503084)
def test_esp32c3_reference_place_reads_back_retired_route_state(
    store: Store, seed: int, record_property: Any
) -> None:
    with FIXTURE.open(encoding="utf-8") as fh:
        design: dict[str, Any] = json.load(fh)

    pcb = PcbHandler(hub=Hub(store=store))
    created = pcb.put(id="esp32c3-ref", args=design)
    assert "created" in created.body
    ref = store.get_ref(kind="pcb", id="esp32c3-ref")
    assert ref is not None

    # --- place ------------------------------------------------------
    # No `iters=` override: the default schedule completes this whole
    # test (author + place + drain + readback) well inside the `slow`
    # budget, so there is no runtime/fidelity tradeoff to make here:
    # cutting iters further would only make the measurement less
    # representative of what put(op='place') actually does for an agent,
    # for no speed win worth taking.
    place_resp = pcb.put(id="esp32c3-ref", args={"op": "place", "seed": seed})
    assert "enqueued" in place_resp.body
    _drain_one_job(store, ref.id)

    # --- the unrouted state is the expected one ----------------------
    # No route ran: every fanout>=2 net stays stored 'unrouted'.
    # (Was the routed-count assertion — the honest population stays
    # fanout >= 2, 11 of the 20 nets here, computed off the fixture so a
    # fixture edit can't drift the two apart.)
    fanout = _fanout(design["connections"])
    fanout2_nets = {name for name, n in fanout.items() if n >= 2}
    assert len(fanout2_nets) == 11

    # --- read back: unrouted status + the retired-DRC answer -----------
    status_rows = store.pcb_route_status(ref.id)
    non_unrouted = [
        (row["name"], row["status"])
        for row in status_rows
        if row["name"] in fanout2_nets and row["status"] != "unrouted"
    ]
    assert not non_unrouted, (
        f"expected every fanout>=2 net 'unrouted' (no route ran), got {non_unrouted}"
    )
    unrouted_fanout2 = sum(1 for row in status_rows if row["name"] in fanout2_nets)

    # view='drc' on this all-synthesized, no-copper board answers the
    # no-copper body — the intended contract (module docstring), asserted
    # rather than a findings table there is no real geometry to check.
    drc = pcb.get(id="esp32c3-ref", view="drc")
    assert "no copper stored" in drc.body
    assert "placer='topo'" in drc.body

    print(f"REPORT seed={seed} unrouted_fanout2={unrouted_fanout2}/{len(fanout2_nets)}")
    record_property("unrouted_fanout2", unrouted_fanout2)
