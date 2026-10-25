"""topo-place vs anneal+vroute bench (``tests/fixtures/pcb/topo_bench.py``).

Three layers:

- verdict/render unit tests — fabricated rows, no DB, no network;
- ``test_topo_handoff_routes_a_tiny_board`` — the pose hand-off's live
  half on a four-part board: the ``placer='topo'`` job completes and
  stamps its stage metrics, then the route-only ``place=False`` leg
  refuses with the retirement contract (routing is retired — the (b)
  pipeline's route half is gone until topo grows its own realiser);
- ``test_topo_bench_table`` — opt-in (``PRECIS_PCB_TOPO_BENCH_OUT``,
  set by ``scripts/pcb-topo-bench``): the standing boards plus the
  route-bench set, the markdown table + JSON written to that path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from tests._pcb_outline import closed_outline
from tests.fixtures.pcb import topo_bench as tb

_TINY: dict[str, Any] = {
    "components": [
        {"refdes": "A", "label": "ic", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "B", "label": "ic", "x": 2.0, "y": 2.0, "pins": [{"name": "1"}]},
        {"refdes": "C", "label": "ic", "x": 0.0, "y": 2.0, "pins": [{"name": "1"}]},
        {"refdes": "D", "label": "ic", "x": 2.0, "y": 0.0, "pins": [{"name": "1"}]},
    ],
    "nets": [{"name": "N1"}, {"name": "N2"}],
    "connections": [
        {"net": "N1", "refdes": "A", "pin": "1"},
        {"net": "N1", "refdes": "B", "pin": "1"},
        {"net": "N2", "refdes": "C", "pin": "1"},
        {"net": "N2", "refdes": "D", "pin": "1"},
    ],
    "features": [closed_outline()],
}


def _row(**kw: Any) -> tb.TopoBenchRow:
    return tb.TopoBenchRow(
        board=kw.pop("board", "b"),
        anneal=kw.pop("anneal", tb.PipeSide()),
        topo=kw.pop("topo", tb.PipeSide()),
        topo_stage=kw.pop("topo_stage", tb.TopoStage()),
        **kw,
    )


def test_verdicts_pass_when_topo_is_no_worse_and_faster() -> None:
    row = _row(
        anneal=tb.PipeSide(
            routed=10, nets=10, drc_errors=2, median_mm=30.0, vias=8, wall_s=20.0
        ),
        topo=tb.PipeSide(
            routed=10, nets=10, drc_errors=2, median_mm=28.0, vias=8, wall_s=12.0
        ),
    )
    vs = tb.verdicts(row)
    assert vs == {
        "routed": "PASS",
        "drc_errors": "PASS",
        "median_mm": "PASS",
        "vias": "PASS",
        "wall_s": "PASS",
    }
    assert tb.overall(row) == "PASS"


def test_verdicts_fail_on_each_metric() -> None:
    base = _row(
        anneal=tb.PipeSide(
            routed=10, nets=10, drc_errors=2, median_mm=30.0, vias=8, wall_s=20.0
        ),
        topo=tb.PipeSide(
            routed=10, nets=10, drc_errors=2, median_mm=30.0, vias=8, wall_s=20.0
        ),
    )
    assert tb.overall(base) == "FAIL"  # equal wall is not faster
    row = _row(
        anneal=tb.PipeSide(
            routed=10, nets=10, drc_errors=0, median_mm=30.0, vias=8, wall_s=20.0
        ),
        topo=tb.PipeSide(
            routed=9, nets=10, drc_errors=1, median_mm=31.0, vias=9, wall_s=10.0
        ),
    )
    vs = tb.verdicts(row)
    assert vs["routed"] == "FAIL"
    assert vs["drc_errors"] == "FAIL"
    assert vs["median_mm"] == "FAIL"
    assert vs["vias"] == "FAIL"
    assert vs["wall_s"] == "PASS"
    assert tb.overall(row) == "FAIL"


def test_verdicts_na_when_a_side_lacks_numbers() -> None:
    row = _row(
        anneal=tb.PipeSide(
            routed=10, nets=10, drc_errors=0, median_mm=30.0, vias=8, wall_s=20.0
        ),
        topo=tb.PipeSide(note="topo place refused: gate"),
    )
    assert set(tb.verdicts(row).values()) == {"n/a"}
    assert tb.overall(row) == "n/a"
    # A refused side that still holds partial numbers compares what it has.
    partial = _row(
        anneal=tb.PipeSide(
            routed=10, nets=10, drc_errors=0, median_mm=30.0, vias=8, wall_s=20.0
        ),
        topo=tb.PipeSide(routed=10, nets=10, note="no drc yet"),
    )
    vs = tb.verdicts(partial)
    assert vs["routed"] == "PASS"
    assert vs["drc_errors"] == "n/a"
    assert tb.overall(partial) == "n/a"


def test_render_tables_mark_na_and_notes() -> None:
    row = _row(
        anneal=tb.PipeSide(
            routed=9, nets=10, drc_errors=1, median_mm=31.2, vias=8, wall_s=20.0
        ),
        topo=tb.PipeSide(note="topo place refused: 3 placement DRC"),
        topo_stage=tb.TopoStage(place_s=4.2),
    )
    table = tb.render_table([row])
    assert "| b | anneal+vroute | 9/10 | 1 | 31.2 | 8 | 20.0 |" in table
    assert "topo+vroute | n/a/n/a | n/a | n/a | n/a | n/a" in table
    assert "topo place refused: 3 placement DRC" in table
    stage = tb.render_stage_table([row])
    assert (
        "| b | n/a | n/a | n/a | n/a | 4.2 | n/a (slice 2) | n/a (slice 2) |" in stage
    )
    verdict_table = tb.render_verdicts([row])
    assert "| b | n/a | n/a | n/a | n/a | n/a | n/a |" in verdict_table


def test_render_tables_fill_when_topo_route_exists() -> None:
    """The column contract slice 5 fills: no shape change, real numbers."""
    row = _row(
        anneal=tb.PipeSide(
            routed=9, nets=10, drc_errors=1, median_mm=31.2, vias=8, wall_s=20.0
        ),
        topo=tb.PipeSide(
            routed=10, nets=10, drc_errors=0, median_mm=29.9, vias=7, wall_s=15.0
        ),
        topo_stage=tb.TopoStage(
            crossings_start=41,
            crossings=0,
            evals=20000,
            evals_per_s=1500.0,
            airwire_mm=120.0,
            place_s=13.3,
        ),
    )
    table = tb.render_table([row])
    assert "| b | topo+vroute | 10/10 | 0 | 29.9 | 7 | 15.0 |" in table
    stage = tb.render_stage_table([row])
    assert "| b | 41 → 0 | 20000 | 1500 | 120 | 13.3 |" in stage
    assert "| b | PASS | PASS | PASS | PASS | PASS | PASS |" in tb.render_verdicts(
        [row]
    )


def test_source_provenance_renders_in_board_cell() -> None:
    row = _row(board="heater-base-test", source="pb460559")
    assert "| heater-base-test (pb460559) | anneal+vroute |" in tb.render_table([row])
    assert "| heater-base-test (pb460559) |" in tb.render_verdicts([row])
    bare = _row(board="nano", source="fixture")
    assert "| nano (fixture) | anneal+vroute |" in tb.render_table([bare])


def test_bench_json_shape_and_provenance() -> None:
    row = _row(
        source="pb460559",
        anneal=tb.PipeSide(
            routed=10, nets=10, drc_errors=0, median_mm=30.0, vias=8, wall_s=20.0
        ),
        topo=tb.PipeSide(note="n/a (pre-slice-5)"),
        topo_stage=tb.TopoStage(crossings=3, evals=20000, place_s=9.9),
    )
    doc = tb.bench_json([row], generated_utc="2026-10-25T00:00:00Z")
    assert doc["schema"] == "precis-pcb-topo-bench/1"
    assert doc["route_mode"] == tb.TOPO_ROUTE_MODE
    (board,) = doc["boards"]
    assert board["source"] == "pb460559"
    assert board["anneal"]["routed"] == 10
    assert board["topo"]["routed"] is None
    assert board["topo_stage"]["via_bracket"] == "n/a (slice 2)"
    assert board["overall"] == "n/a"
    json.dumps(doc)  # serialisable verbatim


def test_power_net_names_class_domain_current_and_rail_names() -> None:
    nets: list[dict[str, Any]] = [
        {"name": "GND"},
        {"name": "3V3"},
        {"name": "12V_POWER"},
        {"name": "PV+"},
        {"name": "SDA"},
        {"name": "U1_7"},
        {"name": "P1", "net_class": "power"},
        {"name": "P2", "domain": "power"},
        {"name": "P3", "est_current_a": 1.5},
        {"name": "plane1", "net_class": "gnd"},
    ]
    assert tb.power_net_names(nets) == {
        "GND",
        "3V3",
        "12V_POWER",
        "PV+",
        "P1",
        "P2",
        "P3",
        "plane1",
    }


def test_power_split_renders_and_drives_the_verdict() -> None:
    """A rail-dominated via count must not skew the flip verdict: the
    split columns show ``sig+pow`` and the bar compares signal-side."""
    row = _row(
        anneal=tb.PipeSide(
            routed=10,
            nets=10,
            drc_errors=0,
            median_mm=30.0,
            vias=90,
            vias_signal=15,
            vias_power=75,
            median_mm_signal=29.0,
            median_mm_power=40.0,
            wall_s=20.0,
        ),
        topo=tb.PipeSide(
            routed=10,
            nets=10,
            drc_errors=0,
            median_mm=28.0,
            vias=80,
            vias_signal=12,
            vias_power=68,
            median_mm_signal=27.0,
            median_mm_power=35.0,
            wall_s=12.0,
        ),
    )
    table = tb.render_table([row])
    assert "| 29.0+40.0p | 15+75p |" in table
    assert "| 27.0+35.0p | 12+68p |" in table
    # Totals would fail (90 → 80 vias passes anyway) — the signal split
    # is what the bar reads: 15 → 12 PASS either way here, so pin the
    # adverse case instead.
    row2 = _row(
        anneal=tb.PipeSide(
            routed=10,
            nets=10,
            drc_errors=0,
            vias=90,
            vias_signal=15,
            vias_power=75,
            wall_s=20.0,
        ),
        topo=tb.PipeSide(
            routed=10,
            nets=10,
            drc_errors=0,
            vias=60,
            vias_signal=16,
            vias_power=44,
            wall_s=12.0,
        ),
    )
    assert tb.verdicts(row2)["vias"] == "FAIL"  # 16 > 15 signal vias


def test_topo_handoff_routes_a_tiny_board(store: Store) -> None:
    """The (b) pipeline's live half: placer='topo' writes poses and the
    stage reports real numbers; the route-only job that used to realize
    them refuses — routing is retired (gr515107), so the refusal IS the
    contract this smoke now pins."""
    pcb = PcbHandler(hub=Hub(store=store))
    assert "created" in pcb.put(id="tiny-topo", args=_TINY).body
    # force=True rides the place gate's escape hatch: the proxy placer
    # prices crossings + length only, so its pose can carry a placement
    # error the never-worse gate would refuse.
    side, stage = tb._topo_side(store, pcb, "tiny-topo", seed=1, effort=200, force=True)
    assert stage.crossings is not None and stage.evals is not None, side.note
    assert side.routed is None and side.nets is None
    assert "topo route refused" in side.note
    assert "routing is retired" in side.note


# Opt-in only (scripts/pcb-topo-bench): the whole set runs two real
# job-path pipelines per board — heater's anneal+route alone passes the
# 300s global. 1h ceiling so a hung run still fails loudly.
@pytest.mark.slow
@pytest.mark.timeout(3600)
@pytest.mark.skipif(
    not os.environ.get("PRECIS_PCB_TOPO_BENCH_OUT"),
    reason="opt-in: scripts/pcb-topo-bench sets PRECIS_PCB_TOPO_BENCH_OUT",
)
def test_topo_bench_table(store: Store) -> None:
    rows = tb.run_bench(store)
    out = Path(os.environ["PRECIS_PCB_TOPO_BENCH_OUT"])
    out.parent.mkdir(parents=True, exist_ok=True)
    from datetime import UTC, datetime

    generated = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out.write_text(
        f"pcb topo-place vs anneal+vroute — {generated} UTC\n\n"
        + tb.render(rows)
        + "\n",
        encoding="utf-8",
    )
    out.with_suffix(".json").write_text(
        json.dumps(tb.bench_json(rows, generated_utc=generated), indent=1) + "\n",
        encoding="utf-8",
    )
    print(tb.render(rows))
    assert rows
    assert all(r.anneal.nets is None or r.anneal.nets > 0 for r in rows)
