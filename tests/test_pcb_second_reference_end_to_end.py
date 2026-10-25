"""Motor/power-board second reference vehicle — place measured for real,
seeds 1-5, mirroring ``tests/test_pcb_reference_end_to_end.py``'s
structure.

**Routing is retired** (gr515107, Reto 2026-10-25: the vector router was
deleted, tag ``vroute-final``; the topological router is the only router
and its realiser is not landed yet). The ``put(op='route')`` leg this
file used to drive — and the paired DRC/routed-count measurement it
existed to report (docs/backlog/pcb-engine-plan.md's §"Obligations to
the paper" item 3) — died with it. Every fanout>=2 net now reads
``unrouted`` and that is the expected state, not a failure; and on this
no-copper board ``view='drc'`` answers a pads-only pass whose only
errors are the per-net ``unrouted`` rows — the retired router's expected
signature, asserted as such below.

What remains: the place job must still drain cleanly across seeds 1-5 —
per-seed parametrization rather than one 5-seed loop for the same reason
the nano sweep does it (a single test pinned whole to one shard can't be
balanced away, gr495187). The structural pins live in the sibling,
``test_pcb_second_reference_design.py``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from precis.workers.executors._common import TERMINAL, current_status
from precis.workers.executors.job_inproc import run_job_inproc_pass

pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).parent / "fixtures" / "pcb" / "motor_power_reference.json"

_SEEDS = (1, 2, 3, 4, 5)


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
            assert status == "succeeded", (
                f"job {job_ref_id} failed to drain cleanly: status={status!r}"
            )
            return
        result = run_job_inproc_pass(store, limit=1)
        assert result["claimed"] == 1, f"expected a queued job, got {result}"
    raise AssertionError(f"job {job_ref_id} never reached a terminal status")


@pytest.mark.timeout(300)  # CI worst ~120s x2.5 for runner variance (gr503084)
@pytest.mark.parametrize("seed", _SEEDS)
def test_motor_power_board_place_seed(store: Store, seed: int) -> None:
    """One test per seed, not one 5-seed loop — the same discipline the
    nano sweep uses (per-seed ids spread over the shards; a single test
    pinned whole to one shard can't be balanced away, gr495187)."""
    with FIXTURE.open(encoding="utf-8") as fh:
        design: dict[str, Any] = json.load(fh)

    fanout = _fanout(design["connections"])
    fanout2_nets = {name for name, n in fanout.items() if n >= 2}

    pcb = PcbHandler(hub=Hub(store=store))

    slug = f"motor-power-ref-s{seed}"
    created = pcb.put(id=slug, args=design)
    assert "created" in created.body
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None

    t0 = time.perf_counter()
    place_resp = pcb.put(id=slug, args={"op": "place", "seed": seed})
    assert "enqueued" in place_resp.body
    _drain_one_job(store, ref.id)
    runtime_s = time.perf_counter() - t0

    # Routing is retired (gr515107): every fanout>=2 net stays 'unrouted'
    # — the expected state, not a failure. Was the routed-count
    # measurement; the honest population stays fanout >= 2, computed off
    # the fixture so a fixture edit can't drift the two apart.
    status_rows = store.pcb_route_status(ref.id)
    non_unrouted = [
        (row["name"], row["status"])
        for row in status_rows
        if row["name"] in fanout2_nets and row["status"] != "unrouted"
    ]
    assert not non_unrouted, (
        f"expected every fanout>=2 net 'unrouted' (routing is retired), "
        f"got {non_unrouted}"
    )

    # view='drc' on this no-copper board runs a pads-only pass whose
    # only errors are the per-net `unrouted` rows — the retired router's
    # expected signature (a board with real cached pads gets a real
    # findings table, not the "no realized copper yet" bail).
    drc = pcb.get(id=slug, view="drc")
    assert "(pads-only DRC — no routed copper yet)" in drc.body
    error_rules = {
        cols[2]
        for line in drc.body.splitlines()
        if len(cols := line.split("\t")) > 2 and cols[1] == "error"
    }
    assert error_rules == {"unrouted"}, f"unexpected error rules: {error_rules}"

    # ---- report: printed for the human, not hidden behind -q ----------
    print(
        f"\n\n# motor_power_reference — seed {seed}\n"
        f"seed={seed}  unrouted_fanout2={len(fanout2_nets)}  "
        f"runtime={round(runtime_s, 2)}s"
    )
