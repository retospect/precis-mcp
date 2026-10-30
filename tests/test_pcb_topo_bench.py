"""topo-place vs anneal+vroute bench (``tests/fixtures/pcb/topo_bench.py``).

Three layers:

- verdict/render unit tests — fabricated rows, no DB, no network;
- ``test_topo_place_writes_copper`` — the slice-5 contract end-to-end
  on a four-part board: ONE ``placer='topo'`` job commits the pose,
  routes it in-process and writes the copper + ``pcb_routes`` rows
  itself (``meta.topo_run.route`` reports the counts);
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
    assert "topo+route | n/a/n/a | n/a | n/a | n/a | n/a" in table
    assert "topo place refused: 3 placement DRC" in table
    stage = tb.render_stage_table([row])
    assert (
        "| b | n/a | n/a | n/a | n/a | 4.2 | n/a (slice 2) | n/a (slice 2) | n/a |"
        in stage
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
    assert "| b | topo+route | 10/10 | 0 | 29.9 | 7 | 15.0 |" in table
    stage = tb.render_stage_table([row])
    assert "| b | 41 → 0 | 20000 | 1500 | 120 | 13.3 |" in stage
    assert "n/a (slice 2) | n/a (slice 2) | n/a |" in stage
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
    assert board["topo_stage"]["corridor_overflow"] is None
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


def test_topo_place_writes_copper(store: Store) -> None:
    """The slice-5 contract end-to-end: ONE placer='topo' job commits
    the pose, routes it in-process and writes the copper + pcb_routes
    rows itself — no second op='route' leg exists."""
    pcb = PcbHandler(hub=Hub(store=store))
    assert "created" in pcb.put(id="tiny-topo", args=_TINY).body
    # force=True rides the place gate's escape hatch: the proxy placer
    # prices crossings + length only, so its pose can carry a placement
    # error the never-worse gate would refuse.
    side, stage = tb._topo_side(store, pcb, "tiny-topo", seed=1, effort=200, force=True)
    assert stage.crossings is not None and stage.evals is not None, side.note
    assert side.nets == 2, side.note
    assert side.routed is not None and side.routed > 0, side.note
    ref = store.get_ref(kind="pcb", id="tiny-topo")
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    assert any(r["ctype"] == "track" for r in store.pcb_copper_list(board_id))
    # The route stage's own report rides the job ref's meta.
    with store.pool.connection() as conn:
        job = conn.execute(
            "SELECT meta FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (ref.id,),
        ).fetchone()
    assert job is not None
    route_meta = (job[0].get("topo_run") or {}).get("route") or {}
    assert route_meta.get("nets") == 2
    assert route_meta.get("realized", 0) > 0
    assert route_meta.get("tracks", 0) > 0
    # the corridor column is the refusal count — 0 on a route that ran
    assert route_meta.get("corridor_overflow") == 0


def test_topo_route_widen_oracle_respects_class_min_clearance(
    store: Store,
) -> None:
    """gr520904 S6 follow-up on the job seam: ``_topo_route``'s widen
    oracle must judge against the resolved per-net rules — a class's
    hard ``min_clearance_mm`` is an error tier the fab floor never
    sees. The fixed-pose board runs a GND rail 0.94 mm under an
    hv-classed pad: the capability floor would still take the 1.0 mm
    ladder step (0.44 gap), the class floor forces 0.25."""
    design: dict[str, Any] = {
        "footprints": [
            {
                "name": "sip2w",
                "pads": [
                    {
                        "pin": "1",
                        "shape": "rect",
                        "x": -1.0,
                        "y": 0.0,
                        "w": 0.8,
                        "h": 1.0,
                    },
                    {
                        "pin": "2",
                        "shape": "rect",
                        "x": 1.0,
                        "y": 0.0,
                        "w": 0.8,
                        "h": 1.0,
                    },
                ],
            }
        ],
        "components": [
            {
                "refdes": r,
                "label": "hdr",
                "footprint": "sip2w",
                "x": x,
                "y": y,
                "fixed": "both",
                "pins": [{"name": "1"}, {"name": "2"}],
            }
            for r, x, y in (
                ("G1", 8.0, 5.0),
                ("G2", 22.0, 5.0),
                ("S1", 15.0, 6.44),
                ("S2", 27.0, 6.44),
            )
        ],
        "nets": [
            {"name": "GND", "net_class": "gnd"},
            {"name": "SIG", "net_class": "hv"},
        ],
        "connections": [
            {"net": "GND", "refdes": "G1", "pin": "2"},
            {"net": "GND", "refdes": "G2", "pin": "1"},
            {"net": "SIG", "refdes": "S1", "pin": "2"},
            {"net": "SIG", "refdes": "S2", "pin": "1"},
        ],
        "net_classes": {"hv": {"min_clearance_mm": 0.8}},
        "features": [closed_outline(0.0, 0.0, 30.0, 15.0)],
    }
    pcb = PcbHandler(hub=Hub(store=store))
    assert "created" in pcb.put(id="widen-cls", args=design).body
    side, _stage = tb._topo_side(
        store, pcb, "widen-cls", seed=1, effort=200, force=True
    )
    assert side.routed == 2, side.note
    ref = store.get_ref(kind="pcb", id="widen-cls")
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    gnd_widths = {
        r["width_mm"]
        for r in store.pcb_copper_list(board_id)
        if r["ctype"] == "track" and r["net"] == "GND"
    }
    assert gnd_widths == {0.25}


def _latest_job(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        job = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (ref_id,),
        ).fetchone()
    assert job is not None
    return int(job[0])


def _copper_key(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, default=str)


def test_topo_route_is_opt_in_default_off(store: Store) -> None:
    """GRIPE_FIX ruling: routing is opt-in — a ``placer='topo'`` job
    with no ``route`` arg places only: stored copper stays untouched
    and ``meta.topo_run.route`` is absent."""
    pcb = PcbHandler(hub=Hub(store=store))
    assert "created" in pcb.put(id="tiny-topo-noroute", args=_TINY).body
    ref = store.get_ref(kind="pcb", id="tiny-topo-noroute")
    assert ref is not None
    enq = pcb.put(
        id="tiny-topo-noroute",
        args={
            "op": "place",
            "placer": "topo",
            "iters": 200,
            "seed": 1,
            "force": True,
        },
    )
    assert "enqueued" in enq.body
    _job, status, _wall = tb._drain(store, ref.id)
    assert status == tb.SUCCEEDED
    board_id = store.pcb_ensure_board(ref.id)
    assert store.pcb_copper_list(board_id) == []
    topo_run = tb._job_meta(store, _latest_job(store, ref.id)).get("topo_run") or {}
    assert "route" not in topo_run


def test_topo_route_empty_result_keeps_stored_copper(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty route result never wipes stored copper: the copper
    replace AND the ``pcb_routes`` rewrite are skipped together, so
    ``view='route-status'`` still matches the kept copper, and the
    summary reports ``route produced nothing; stored copper kept``."""
    import types

    from precis.pcb import realize as pcb_realize
    from precis.pcb.topoplace import realise as _tprealise

    pcb = PcbHandler(hub=Hub(store=store))
    assert "created" in pcb.put(id="tiny-topo-empty", args=_TINY).body
    side, _stage = tb._topo_side(
        store, pcb, "tiny-topo-empty", seed=1, effort=200, force=True
    )
    ref = store.get_ref(kind="pcb", id="tiny-topo-empty")
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    seeded = store.pcb_copper_list(board_id)
    assert any(r["ctype"] == "track" for r in seeded), side.note
    seeded_routes = store.pcb_routes_get(ref.id)

    empty_plan = types.SimpleNamespace(
        pieces=(),
        vias=(),
        layers=(),
        crossings=(),
        unreachable=(),
        stranded=(),
        no_via=(),
        guarded=(),
        severed=(),
    )
    monkeypatch.setattr(
        _tprealise,
        "route_design",
        lambda *a, **k: (empty_plan, pcb_realize.RealizeResult((), (), ())),
    )
    # a different seed guarantees a fresh job (the idem key covers
    # params + design state, so a repeat run could collapse onto run 1)
    tb._topo_side(store, pcb, "tiny-topo-empty", seed=2, effort=200, force=True)
    copper = store.pcb_copper_list(board_id)
    assert sorted(map(_copper_key, copper)) == sorted(map(_copper_key, seeded))
    # the per-net status rows are kept verbatim — they describe the
    # stored copper, which the empty route left in place
    routes = store.pcb_routes_get(ref.id)
    assert {k: _copper_key(v) for k, v in routes.items()} == {
        k: _copper_key(v) for k, v in seeded_routes.items()
    }
    job_id = _latest_job(store, ref.id)
    route_meta = (tb._job_meta(store, job_id).get("topo_run") or {}).get("route")
    assert route_meta is not None and route_meta.get("nets") == 2
    # gr540907: the meta may not silently disagree with the stored rows —
    # a skipped write-back says so and names the kept tallies.
    assert route_meta["persisted"] is False
    assert route_meta["stored"]["realized"] == sum(
        1 for r in seeded_routes.values() if r.get("status") == "realized"
    )
    assert route_meta["stored"]["tracks"] == sum(
        1 for r in seeded if r["ctype"] == "track"
    )
    assert "route produced nothing; stored copper kept" in tb.job_summary_text(
        store, job_id
    )


def test_topo_route_copper_and_route_rows_write_atomically(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pcb_copper_replace`` + ``pcb_routes_write`` share one tx: a
    routes-write raise rolls the copper replace back — the seeded
    copper survives a route stage that would have replaced it, and the
    raise lands in the summary line with the exception text."""
    import types

    from precis.pcb import realize as pcb_realize
    from precis.pcb.topoplace import realise as _tprealise

    pcb = PcbHandler(hub=Hub(store=store))
    assert "created" in pcb.put(id="tiny-topo-atomic", args=_TINY).body
    side, _stage = tb._topo_side(
        store, pcb, "tiny-topo-atomic", seed=1, effort=200, force=True
    )
    ref = store.get_ref(kind="pcb", id="tiny-topo-atomic")
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    seeded = store.pcb_copper_list(board_id)
    assert any(r["ctype"] == "track" for r in seeded), side.note

    # A different result than the seeded copper — if the replace had
    # committed on its own, this marker width would be what survives.
    marker = pcb_realize.RealizedTrack(
        seg_id=0,
        net_id=0,
        layer=0,
        segments=({"shape": "line", "start": [0.0, 0.0], "end": [1.0, 1.0]},),
        length_mm=1.0,
        blocked_by=None,
        width_mm=0.99,
    )
    plan = types.SimpleNamespace(
        pieces=(),
        vias=(),
        layers=(),
        crossings=(),
        unreachable=(),
        stranded=(),
        no_via=(),
        guarded=(),
        severed=(),
    )
    monkeypatch.setattr(
        _tprealise,
        "route_design",
        lambda *a, **k: (plan, pcb_realize.RealizeResult((marker,), (), ())),
    )

    def _boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("boom mid-write")

    monkeypatch.setattr(store, "pcb_routes_write", _boom)
    tb._topo_side(store, pcb, "tiny-topo-atomic", seed=2, effort=200, force=True)
    copper = store.pcb_copper_list(board_id)
    assert sorted(map(_copper_key, copper)) == sorted(map(_copper_key, seeded))
    assert not any(r.get("width_mm") == 0.99 for r in copper)
    job_id = _latest_job(store, ref.id)
    assert "topo-route: raised RuntimeError: boom mid-write" in tb.job_summary_text(
        store, job_id
    )


def test_topo_route_raise_fails_the_job_loudly(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1: a raised route-stage error fails the job — never 'succeeded,
    0 copper'. ``meta.topo_run.route.error`` carries the raise text and
    the bench row's note names a route failure, not a place-gate
    refusal."""
    from precis.pcb.topoplace import realise as _tprealise

    pcb = PcbHandler(hub=Hub(store=store))
    assert "created" in pcb.put(id="tiny-topo-raise", args=_TINY).body

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("mesh exploded")

    monkeypatch.setattr(_tprealise, "route_design", _boom)
    side, _stage = tb._topo_side(
        store, pcb, "tiny-topo-raise", seed=1, effort=200, force=True
    )
    assert side.routed is None
    assert side.note == "topo route stage raised: RuntimeError: mesh exploded"
    ref = store.get_ref(kind="pcb", id="tiny-topo-raise")
    assert ref is not None
    job_id = _latest_job(store, ref.id)
    route_meta = (tb._job_meta(store, job_id).get("topo_run") or {}).get("route")
    assert route_meta == {"error": "RuntimeError: mesh exploded"}


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


# ── _drain strict/lenient (gr524304) ────────────────────────────────────


def _drain_fixture_refs(store: Store) -> tuple[int, int, int]:
    """A board parent + the just-enqueued target job + a stray queued job
    sharing the dev DB."""
    parent = store.insert_ref(kind="pcb", slug="drain-gr524304", title="b", meta={})
    target = store.insert_ref(kind="job", slug=None, title="target", meta={})
    stray = store.insert_ref(kind="job", slug=None, title="stray", meta={})
    return int(parent.id), int(target.id), int(stray.id)


def test_drain_lenient_tolerates_stray_claims(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """gr524304: strict=False keeps passing while a stray queued job
    shares the dev DB — the target still finishes and _drain returns,
    noting the unexpected claim count instead of failing AFTER the real
    work already ran."""
    from precis.store import Tag

    parent_id, target_id, _stray_id = _drain_fixture_refs(store)
    calls = {"n": 0}

    def _fake_pass(_store: Store, *, limit: int) -> dict[str, int]:
        calls["n"] += 1
        if calls["n"] == 1:
            # The stray shares the pass: two claims where the strict
            # assert expects exactly one.
            return {"claimed": 2, "ok": 2, "failed": 0}
        store.add_tag(
            target_id,
            Tag.parse_strict("STATUS:succeeded", kind="job"),
            set_by="agent",
            replace_prefix=True,
        )
        return {"claimed": 1, "ok": 1, "failed": 0}

    monkeypatch.setattr(tb, "run_job_inproc_pass", _fake_pass)

    job_id, status, wall = tb._drain(
        store, parent_id, target_job_id=target_id, strict=False
    )

    assert (job_id, status) == (target_id, tb.SUCCEEDED)
    assert wall >= 0.0
    assert "claimed 2 job(s), not 1" in capsys.readouterr().out


def test_drain_strict_still_raises_on_stray_claims(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default stays exact: one pass claiming anything but exactly
    one job is the AssertionError the bench has always raised."""
    parent_id, target_id, _stray_id = _drain_fixture_refs(store)

    def _fake_pass(_store: Store, *, limit: int) -> dict[str, int]:
        return {"claimed": 2, "ok": 2, "failed": 0}

    monkeypatch.setattr(tb, "run_job_inproc_pass", _fake_pass)

    with pytest.raises(AssertionError, match="expected a queued job"):
        tb._drain(store, parent_id, target_job_id=target_id)


def test_topo_route_all_withdrawn_writes_failed_rows_and_not_run(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr537831 review: an emit set withdrawn whole produced no
    copper, and the route stage used to skip BOTH writes — no
    ``pcb_routes`` rows meant every net read 'unrouted' by default and
    the bench's unrecorded DRC counted as a clean 0. Now the run's
    failed rows land even with empty copper (stored copper is still
    never wiped), and ``_measure`` reports a missing DRC run as
    'DRC not run'."""
    import types

    from precis.pcb import realize as pcb_realize
    from precis.pcb.topoplace import realise as _tprealise

    pcb = PcbHandler(hub=Hub(store=store))
    assert "created" in pcb.put(id="tiny-topo-severed", args=_TINY).body
    severed_plan = types.SimpleNamespace(
        pieces=(),
        vias=(),
        layers=(),
        crossings=(),
        unreachable=(),
        stranded=(),
        no_via=(),
        guarded=(),
        severed=(0, 1),
    )
    monkeypatch.setattr(
        _tprealise,
        "route_design",
        lambda *a, **k: (severed_plan, pcb_realize.RealizeResult((), (), ())),
    )
    side, _stage = tb._topo_side(
        store, pcb, "tiny-topo-severed", seed=1, effort=200, force=True
    )
    ref = store.get_ref(kind="pcb", id="tiny-topo-severed")
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    assert store.pcb_copper_list(board_id) == []
    # the run's verdict landed: every multi-member net is failed, not
    # 'unrouted' by default.
    status = {r["name"]: r["status"] for r in store.pcb_route_status(ref.id)}
    assert status == {"N1": "failed", "N2": "failed"}
    # the bench never reads a missing DRC run as a clean 0.
    assert side.drc_errors is None
    assert side.note == "DRC not run"
