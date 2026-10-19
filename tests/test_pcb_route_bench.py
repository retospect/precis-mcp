"""PCB route benchmark harness (``docs/backlog/pcb-vector-router.md``
slice 0). The boards and how each is measured: ``tests/fixtures/pcb/route_bench.py``.

Two tests:

- ``test_qfp_plaza_bench_is_deterministic`` always runs (slow lane): the
  DB-free plaza member measured twice gives the same row, wall time aside.
- ``test_route_bench_table`` routes the whole set and writes the table.
  It is opt-in (``PRECIS_PCB_BENCH_OUT=<path>``, set by
  ``scripts/pcb-route-bench``) because every board on it already runs in
  its own regression test; routing them all again on every CI shard
  would double that cost for a table nobody reads there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from precis.store import Store
from tests.fixtures.pcb import route_bench as rb


@pytest.mark.slow
def test_qfp_plaza_bench_is_deterministic() -> None:
    first = rb.bench_qfp_plaza()
    second = rb.bench_qfp_plaza()
    assert first.key() == second.key()
    assert first.nets == 55
    # The plaza floor of test_pcb_qfp_fanout's scoreboard.
    assert first.routed >= 50


def test_render_table_reports_rate_per_net_and_connection() -> None:
    row = rb.BenchRow(
        board="b",
        nets=4,
        connections=8,
        routed=3,
        failed=1,
        vias=2,
        length_mm=12.34,
        wall_s=2.0,
        drc_s=0.25,
        errors_by_rule={"unrouted": 1},
        warns_by_rule={"clearance": 2, "board_edge_clearance": 1},
    )
    table = rb.render_table([row])
    assert (
        "| b | 3/4 | 1 | 8 | 2 | 12.3 | 2.0 | 0.50 | 0.250 | 0.25 | 1: unrouted 1 "
        "| 3: board_edge_clearance 1, clearance 2 |"
    ) in table


def test_track_length_counts_lines_and_arcs() -> None:
    quarter = {
        "shape": "arc",
        "start": [1.0, 0.0],
        "end": [0.0, 1.0],
        "center": [0.0, 0.0],
        "cw": False,
    }
    line = {"shape": "line", "start": [0.0, 0.0], "end": [3.0, 4.0]}
    assert rb._track_length([line]) == pytest.approx(5.0)
    assert rb._track_length([quarter]) == pytest.approx(1.5707963, rel=1e-6)
    assert rb._track_length([{**quarter, "cw": True}]) == pytest.approx(
        3 * 1.5707963, rel=1e-6
    )


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("PRECIS_PCB_BENCH_OUT"),
    reason="opt-in: scripts/pcb-route-bench sets PRECIS_PCB_BENCH_OUT",
)
def test_route_bench_table(store: Store) -> None:
    rows = [rb.bench_nano(store)]
    rows += [rb.bench_esp32c3(store, seed) for seed in rb.ESP32C3_SEEDS]
    rows.append(rb.bench_ewod_replay(store))
    rows.append(rb.bench_qfp_plaza())
    taz15 = Path(os.environ.get("PRECIS_PCB_BENCH_TAZ15") or rb.TAZ15_BLANK_SNAPSHOT)
    if taz15.exists():
        rows.append(rb.bench_taz15_blank(store, taz15))
    table = rb.render_table(rows)
    failed = "\n".join(
        f"- {r.board}: {', '.join(r.failed_nets)}" for r in rows if r.failed_nets
    )
    out = Path(os.environ["PRECIS_PCB_BENCH_OUT"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"{table}\n\nFailed nets:\n{failed}\n"
        f"\ntaz15-blank snapshot: {taz15 if taz15.exists() else 'absent'}\n",
        encoding="utf-8",
    )
    # Everything but wall time, for scripts/pcb-route-bench's rerun diff.
    out.with_suffix(".json").write_text(
        json.dumps([list(r.key()) for r in rows], indent=1), encoding="utf-8"
    )
    print(table)
    assert all(r.nets > 0 for r in rows)
