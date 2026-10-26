"""pcb-workspace-code slice 4, offline program end-to-end
(docs/backlog/pcb-workspace-code.md, Slices row 4): ``main.py --place
--route`` on a laptop with no network writes the full §Q3 output set for
the §Q1 board (``examples/boards/usbc_attiny``) — with routing retired
(gr515107) the "route" outputs honestly describe the placed board — and
``pcb.local`` wraps the same engines the ``pcb_place`` worker runs — no
DB, no network, no store.
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
    # Routing is retired: the placed board carries no router copper, so
    # copper.json is the honest empty set (gr515107).
    assert copper["copper"] == [] and copper["routes"] == {}
    zf = zipfile.ZipFile(out / "gerbers.zip")
    assert {n for n in zf.namelist() if n.endswith((".gbr", ".drl"))}
    assert (out / "drc.txt").read_text(encoding="utf-8").startswith("usbc-attiny")


def test_local_route_raises_the_retired_refusal() -> None:
    """``local.route`` is kept as a refusing stub so the call names the
    retirement instead of an AttributeError (gr515107)."""
    placement = pcb.local.place(_example("sanity"))
    with pytest.raises(pcb.local.LocalError, match="routing is retired"):
        pcb.local.route(_example("sanity"), placement=placement)
    with pytest.raises(pcb.local.LocalError, match="routing is retired"):
        pcb.local.route(placement, placement=placement)


def test_topo_place_accepts_pcb_objective(tmp_path: Path) -> None:
    """``pcb.objective(...)`` (pcb-intent-api slice 1) is a plain shares
    mapping ``local.topo_place`` resolves the same way a hand-built dict
    always could — but unlike a partial hand-built dict, an unset term
    lands at 0.0, never the ``ObjectiveShares`` class default, so
    ``pcb.objective(ratsnest=1.0)`` really is all-ratsnest."""
    run = pcb.local.topo_place(
        _example("sanity"),
        objective=pcb.objective(ratsnest=1.0),
        effort=200,
        seed=1,
        out=str(tmp_path / "topo"),
    )
    assert run.poses


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
