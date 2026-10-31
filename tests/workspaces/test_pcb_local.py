"""pcb-workspace-code slice 4, offline program end-to-end
(docs/backlog/pcb-workspace-code.md, Slices row 4): ``main.py --place
--route`` on a laptop with no network writes the full §Q3 output set for
the §Q1 board (``examples/boards/usbc_attiny``) — ``--route`` runs the
topo route stage over the anneal's pose so the outputs describe real
copper again (gr513867) — and ``pcb.local`` wraps the same engines the
``pcb_place`` worker runs — no DB, no network, no store.
"""

from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

import precis_geom.pcb as pcb

ROOT = Path(__file__).resolve().parents[2]
BOARDS = ROOT / "examples" / "boards"

pytestmark = pytest.mark.slow


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _example(name: str) -> pcb.Board:
    return _load(BOARDS / name / "main.py", f"example_{name}").build()


def test_usbc_attiny_end_to_end_writes_every_q3_output(tmp_path: Path) -> None:
    out = pcb.run(
        _example("usbc_attiny"), tmp_path / "out", argv=["--place", "--route"]
    )
    expected = {
        "manifest.json",
        "check.txt",
        "netlist.net",
        "schematic.svg",
        "ratsnest.svg",
        "placement.json",
        "copper.json",
        "drc.txt",
        "gerbers.zip",
        "board.svg",
        "bom.csv",
        "cpl.csv",
    }
    assert expected <= {p.name for p in out.iterdir()}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "pcb" and manifest["schema"] == 1
    placement = json.loads((out / "placement.json").read_text(encoding="utf-8"))
    assert len(placement["poses"]) == 11
    copper = json.loads((out / "copper.json").read_text(encoding="utf-8"))
    # --route routes the anneal's pose through the topo stage (gr513867):
    # copper.json carries the route rows + digest it produced.
    assert copper["routes"] and set(copper) >= {
        "copper",
        "routes",
        "unrouted_reasons",
        "digest",
        "committed_round",
    }
    assert set(copper["digest"]) == {"failed", "constraints"}
    zf = zipfile.ZipFile(out / "gerbers.zip")
    assert {n for n in zf.namelist() if n.endswith((".gbr", ".drl"))}
    assert (out / "drc.txt").read_text(encoding="utf-8").startswith("usbc-attiny")


def _tiny_board() -> pcb.Board:
    """Three two-pin nets on a 30x20 board, one of them F↔B — the
    routable control: nothing crosses, so the honest expectation is
    3/3 realized and 0 DRC errors."""
    fp = pcb.Footprint(
        "SIP2",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )
    b = pcb.Board("tiny", stackup=4, outline=pcb.rect(30.0, 20.0))
    f = b.footprint(fp)
    a1 = b.part("A1", footprint=f, pins=["1", "2"]).at(5.0, 5.0)
    a2 = b.part("A2", footprint=f, pins=["1", "2"]).at(15.0, 5.0)
    c1 = b.part("C1", footprint=f, pins=["1", "2"]).at(5.0, 15.0)
    c2 = b.part("C2", footprint=f, pins=["1", "2"]).at(15.0, 15.0, side="bottom")
    e1 = b.part("E1", footprint=f, pins=["1", "2"]).at(25.0, 5.0)
    e2 = b.part("E2", footprint=f, pins=["1", "2"]).at(25.0, 15.0)
    for part in (a1, a2, c1, c2, e1, e2):
        for pin in ("1", "2"):
            part[pin].etype("bidi", fmax_hz=0.0, assume="header pin")
    b.wire(a1["2"], a2["1"], name="N1")
    b.wire(c1["2"], c2["2"], name="N2")
    b.wire(e1["1"], e2["1"], name="N3")
    return b


def test_local_route_authored_poses_returns_a_route_result() -> None:
    """gr513867: ``local.route`` is the in-script route call again —
    ``place=False`` routes the authored poses through the topo stage,
    DB-free, and hands back per-net status, geometry counts and the run
    digest (gr513871's produced half). The sanity board's authored pose
    is the router's honest hard case (LR×TB crossing cycles, a known
    gap), so the assertions pin the CONTRACT — every net accounted for,
    the digest mirroring the failed set — plus ``drc_errors == 0``: the
    widen oracle measures the same footprint-fed model ``drc()`` does
    (a rotated authored footprint's pads are not transposed)."""
    rt = pcb.local.route(_example("sanity"), place=False)
    assert rt.ready and rt.slug == "sanity"
    assert len(rt.nets) == 20
    assert sorted(rt.routed + rt.failed + rt.dangling + rt.unrouted) == sorted(rt.nets)
    assert rt.tracks == len(rt.result.tracks)
    assert rt.drc_errors == 0
    assert rt.digest["failed"] == rt.failed
    if rt.failed:
        assert rt.digest["constraints"]
        for c in rt.digest["constraints"]:
            assert c["reason"] and c["count"] >= 1 and c["nets"]


def test_local_route_routes_a_routable_board_clean() -> None:
    """The clean half of the contract: a board with no crossing wires
    routes every net, mints its via and reports an empty digest."""
    rt = pcb.local.route(_tiny_board(), place=False)
    assert rt.ready and rt.slug == "tiny"
    assert rt.routed == ["N1", "N2", "N3"] and rt.failed == []
    assert rt.tracks > 0 and rt.vias == 1 and rt.length_mm > 0
    assert rt.drc_errors == 0
    assert rt.digest == {"failed": [], "constraints": []}


def test_local_route_routes_a_given_placement() -> None:
    """``placement=`` routes the anneal's pose without a second solve —
    the same ``_route_stage``, the pose the caller already paid for."""
    board = _example("sanity")
    placement = pcb.local.place(board, iters=200)
    rt = pcb.local.route(board, placement=placement)
    assert rt.ready and len(rt.nets) == 20
    assert rt.poses == placement.poses


def test_local_route_placement_and_place_false_disagree() -> None:
    placement = pcb.local.place(_example("sanity"), iters=200)
    with pytest.raises(pcb.local.LocalError, match="disagree"):
        pcb.local.route(placement, placement=placement, place=False)


def test_local_route_refusal_surfaces_as_localerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route-stage refusal raises ``LocalError``, not the engine's
    ``BadInput`` — the docstring's contract on the ``place=False`` and
    ``placement=`` paths (``topo_place`` already wraps its own stage
    best-effort, so the ``place=True`` path reports the failure through
    the run instead)."""
    from precis.pcb.topoplace.wires import CorridorCut, CorridorOverflow

    cut = CorridorCut(
        edge=0,
        demand=3.0,
        cap=1.0,
        owners=("U1", "outline"),
        nets=("N1",),
        x=1.0,
        y=2.0,
    )

    def boom(*args: object, **kwargs: object) -> object:
        raise CorridorOverflow((cut,))

    monkeypatch.setattr("precis.pcb.local._route_stage", boom)
    with pytest.raises(pcb.local.LocalError, match="corridor overflow"):
        pcb.local.route(_tiny_board(), place=False)

    placement = pcb.local.place(_tiny_board(), iters=200)
    with pytest.raises(pcb.local.LocalError, match="corridor overflow"):
        pcb.local.route(_tiny_board(), placement=placement)


def test_board_route_is_the_traced_library_call(tmp_path: Path) -> None:
    """gr513867: ``board.route()`` is the script-side entry — one
    ``core`` frame on the trace like every other library call, and a
    ``LocalRoute`` result."""
    from precis_geom import trace as _trace

    board = _tiny_board()
    with _trace.run(work_root=tmp_path) as tr:
        rt = board.route(place=False)
    assert rt.routed
    quals = [f["qual"] for f in tr.dump()["frames"]]
    assert "pcb.Board.route" in quals


def test_local_route_feeds_the_routed_board_to_drc_and_exports(
    tmp_path: Path,
) -> None:
    """A ``LocalRoute`` is a valid input to ``drc``/``copper_json`` —
    the routed copper, not the empty placed set."""
    rt = pcb.local.route(_tiny_board(), place=False)
    findings = pcb.local.drc(rt)
    assert not [f for f in findings if f.severity == "error"]
    copper = pcb.local.copper_json(rt)
    assert copper["copper"] and len(copper["routes"]) == 3
    assert copper["digest"] == rt.digest


def test_board_default_stackup_is_four_routable_layers() -> None:
    """gr514459: ``Board()`` emits no stackup key and resolves to the
    semantic default — 4 routable copper layers
    (``DEFAULT_STACKUP``); ``stackup=4`` emits the same names verbatim."""
    from precis.pcb import local as kernel
    from precis.pcb.ir import layer_is_routable

    bare = kernel._stackup(pcb.Board("bare").to_batch())
    assert [e["name"] for e in bare] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert all(layer_is_routable(e) for e in bare)

    counted = kernel._stackup(pcb.Board("four", stackup=4).to_batch())
    assert [e["name"] for e in counted] == [e["name"] for e in bare]
    assert all(layer_is_routable(e) for e in counted)


def test_local_kernel_miss_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    def fake_module(name: str) -> ModuleType:
        raise ImportError("No module named 'precis.pcb.local'")

    monkeypatch.setattr(importlib, "import_module", fake_module)
    with pytest.raises(ImportError, match="pcb-kernel"):
        _ = pcb.local


def test_local_route_widen_oracle_sees_mounting_hole_drills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The widen oracle builds its model the way the pcb_place job does
    (``_copper_model``), so mounting-hole drills are in it — a rail is
    never widened toward a hole the job would refuse (gr513867)."""
    from precis.pcb import drc as pcb_drc

    models: list[dict] = []
    real = pcb_drc.run_geometric_drc

    def spy(model, **kw):
        models.append(model)
        return real(model, **kw)

    monkeypatch.setattr(pcb_drc, "run_geometric_drc", spy)
    b = _tiny_board()
    b.hole(28.0, 10.0, drill=2.2, ring=4.0)
    pcb.local.route(b, place=False)
    assert models, "the widen oracle never ran"
    for m in models:
        assert any(
            abs(d["x"] - 28.0) < 1e-6
            and abs(d["y"] - 10.0) < 1e-6
            and abs(d["dia_mm"] - 2.2) < 1e-6
            for d in m["drills"]
        )
