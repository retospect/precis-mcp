"""Real-board route trial for the vector router — a dogfood measurement on
Reto's 140-part EasyEDA board, not a gate (Reto 2026-10-22: re-add the
"does a real board route at all" trial that d42369679 deleted with the
grid router).

Env-gated: ``PRECIS_EPRO_FIXTURE`` names the gitignored ``.epro2`` export,
as a path inside the test container (the worktree mounts at ``/app``):

    PRECIS_EPRO_FIXTURE=/app/epro.local/heaterBaseTest.epro2 \\
        scripts/test -n0 -s tests/test_pcb_epro_route_trial.py

(``-n0``: under xdist the printed numbers never reach the terminal.)

Two configs. ``as-imported``: the import locks only what EasyEDA had
locked, so the route job's placement pass may move the rest before routing.
``frozen``: every instance ``fixed='both'``, so the number is the router
alone on the author's placement. Each run prints routed/failed nets, wall
time, instances moved and copper rows per layer (are the inner layers
used?). The only assert is that the router reported on some nets.
"""

from __future__ import annotations

import os
import pathlib
import time
from collections import Counter

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.ingest import pcb_epro
from precis.pcb import epro
from precis.store import Store
from precis.workers.executors._common import TERMINAL, current_status
from precis.workers.executors.job_inproc import run_job_inproc_pass


def _drain(store: Store, parent_id: int, passes: int = 25) -> None:
    """Run this design's newest job to a terminal status; a failed route
    job (any net failed) is a measurement, not an error."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
    assert row is not None, f"no job queued for parent_id={parent_id}"
    for _ in range(passes):
        with store.pool.connection() as conn:
            if current_status(conn, row[0]) in TERMINAL:
                return
        run_job_inproc_pass(store, limit=1)
    raise AssertionError(f"job {row[0]} never reached a terminal status")


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("PRECIS_EPRO_FIXTURE"),
    reason="set PRECIS_EPRO_FIXTURE=/path/to/real.epro2 to run",
)
@pytest.mark.parametrize("freeze_all", [False, True], ids=["as-imported", "frozen"])
def test_a_real_board_routes_at_all(store: Store, freeze_all: bool) -> None:
    data = pathlib.Path(os.environ["PRECIS_EPRO_FIXTURE"]).read_bytes()
    boards = epro.read_archive(data).by_type("PCB")
    tag = "frozen" if freeze_all else "as-imported"
    result = pcb_epro.import_epro(
        store,
        data,
        slug=f"epro-route-trial-{tag}",
        board_uuid=boards[-1].uuid if len(boards) > 1 else None,
    )
    if freeze_all:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE pcb_instances SET fixed = 'both' "
                "WHERE ref_id = %s AND retired_at IS NULL",
                (result.ref_id,),
            )

    def poses() -> dict[str, tuple[float, float, float, str]]:
        return {
            str(i["refdes"]): (
                float(i["x"] or 0.0),
                float(i["y"] or 0.0),
                float(i["rot"] or 0.0),
                str(i["layer"]),
            )
            for i in store.pcb_graph(result.ref_id)["instances"]
        }

    before = poses()
    pcb = PcbHandler(hub=Hub(store=store))
    enq = pcb.put(id=f"epro-route-trial-{tag}", args={"op": "route", "seed": 1})
    assert "enqueued" in enq.body, enq.body
    started = time.monotonic()
    _drain(store, result.ref_id)
    elapsed = time.monotonic() - started

    after = poses()
    moved = sorted(
        r
        for r, pose in after.items()
        if r in before
        and (
            any(abs(a - b) > 1e-3 for a, b in zip(pose[:3], before[r][:3], strict=True))
            or pose[3] != before[r][3]
        )
    )
    graph = store.pcb_graph(result.ref_id)
    multi = {str(n["name"]) for n in graph["nets"] if len(n["members"]) >= 2}
    rows = [r for r in store.pcb_route_status(result.ref_id) if r["name"] in multi]
    by_status = Counter(str(r["status"]) for r in rows)
    copper = store.pcb_copper_list(store.pcb_ensure_board(result.ref_id))
    per_layer = Counter(
        (str(c["ctype"]), str(c.get("layer"))) for c in copper if not c.get("fixed")
    )
    print(
        f"\n[{tag}] {result.stats['components']} components, {len(multi)} nets "
        f"(fanout >= 2): {elapsed:.1f}s; realized {by_status.get('realized', 0)}, "
        f"failed {by_status.get('failed', 0)}; status {dict(by_status)}"
    )
    print(f"[{tag}] instances moved: {len(moved)} {moved[:8]}")
    print(f"[{tag}] routed copper by (ctype, layer): {dict(sorted(per_layer.items()))}")
    notes = Counter(
        str(r["note"])[:120] for r in rows if r["status"] != "realized" and r["note"]
    )
    for note, n in notes.most_common(3):
        print(f"[{tag}] {n} net(s): {note}")

    assert rows, "the router reported on no nets at all"
