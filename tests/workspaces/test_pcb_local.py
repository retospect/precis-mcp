"""pcb-workspace-code slice 4, offline program end-to-end
(docs/backlog/pcb-workspace-code.md, Slices row 4): ``main.py --place
--route`` on a laptop with no network writes the full §Q3 output set for
the §Q1 board (``examples/boards/usbc_attiny``), and ``pcb.local`` wraps
the same engines the ``pcb_place``/``pcb_route`` workers run — no DB, no
network, no store.
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
    assert copper["copper"]
    zf = zipfile.ZipFile(out / "gerbers.zip")
    assert {n for n in zf.namelist() if n.endswith((".gbr", ".drl"))}
    assert (out / "drc.txt").read_text(encoding="utf-8").startswith("usbc-attiny")


def test_local_route_sanity_board_is_100_percent_routed() -> None:
    placement = pcb.local.place(_example("sanity"))
    routing = pcb.local.route(_example("sanity"), placement=placement)
    assert routing.failed == 0, routing.summary()
    assert routing.errors == 0, pcb.local.findings_text(routing.findings)
    assert routing.routed == routing.nets


def test_local_place_then_route_reuses_the_placed_ir() -> None:
    placement = pcb.local.place(_example("sanity"), iters=800)
    assert placement.poses and placement.ready
    routing = pcb.local.route(placement, placement=placement)
    assert routing.nets > 0


def test_local_kernel_miss_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    def fake_module(name: str) -> ModuleType:
        raise ImportError("No module named 'precis.pcb.local'")

    monkeypatch.setattr(importlib, "import_module", fake_module)
    with pytest.raises(ImportError, match="pcb-kernel"):
        _ = pcb.local
