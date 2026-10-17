"""BOM/CPL account for every part (gr495285).

On usbc-attiny-dogfood-b (12 placed parts) the exported files were complete
but the response previewed only 12 lines, so the 12th row (U2, sorted last)
read as silently omitted. The invariant, over the repo's fixture boards plus
a board of that shape: BOM rows + named exclusions == all instances, CPL
rows == the placed assembled parts, and a cut preview says it is cut.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import export

_FIXTURES = Path(__file__).parent / "fixtures" / "pcb"


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _dogfood_shaped() -> dict[str, Any]:
    """12 distinct-label parts, all placed: 13 CSV lines, one past the old
    12-line preview."""
    refs = [f"C{n}" for n in range(1, 5)] + [f"R{n}" for n in range(1, 5)]
    refs += ["D1", "J1", "U1", "U2"]
    comps = [
        {
            "refdes": r,
            "label": f"{r} value",
            "part": f"C{1000 + n}",
            "footprint": "0603",
            "x": 5.0 + 6.0 * (n % 6),
            "y": 5.0 + 6.0 * (n // 6),
            "pins": [{"name": "1"}, {"name": "2"}],
        }
        for n, r in enumerate(refs)
    ]
    return {"components": comps, "nets": [], "connections": [], "features": []}


def _placed_but_last(board: dict[str, Any]) -> dict[str, Any]:
    """Every part posed except the last, so the CPL must name one unplaced."""
    comps = [dict(c) for c in board["components"]]
    for n, c in enumerate(comps[:-1]):
        c.setdefault("x", 2.0 + n)
        c.setdefault("y", 3.0)
    return {**board, "components": comps}


def _board(name: str) -> dict[str, Any]:
    if name == "dogfood_shaped":
        return _dogfood_shaped()
    raw = (_FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    return _placed_but_last(json.loads(raw))


def _refdes_column(path: Path, *, joined: bool) -> list[str]:
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))[1:]
    if joined:  # BOM: "Designator" is comma-joined, column 1
        return [d for r in rows for d in r[1].split(",")]
    return [r[0] for r in rows]


@pytest.mark.parametrize(
    "name",
    ["dogfood_shaped", "esp32c3_reference", "motor_power_reference", "nano_oc_switch"],
)
def test_bom_and_cpl_account_for_every_part(pcb, tmp_path, name):
    board = _board(name)
    pcb.put(id=name, args=board)
    all_refs = {c["refdes"] for c in board["components"]}
    args = {"dir": str(tmp_path)}
    bom_resp = pcb.get(id=name, view="bom", args=args).body
    cpl_resp = pcb.get(id=name, view="cpl", args=args).body

    bom = _refdes_column(tmp_path / f"{name}-bom.csv", joined=True)
    assert len(bom) == len(set(bom))
    excluded = set(
        re.findall(
            r"\b[A-Z]+\d+\b",
            " ".join(
                ln for ln in bom_resp.splitlines() if "board feature(s) excluded" in ln
            ),
        )
    )
    assert set(bom) | excluded == all_refs
    assert not set(bom) & excluded

    cpl = _refdes_column(tmp_path / f"{name}-cpl.csv", joined=False)
    placed = {c["refdes"] for c in board["components"] if c.get("x") is not None}
    assert set(cpl) == placed - excluded
    for r in all_refs - placed:
        assert r in cpl_resp, f"unplaced {r} not named in the CPL response"

    for resp, view in ((bom_resp, "bom"), (cpl_resp, "cpl")):
        file_lines = (
            (tmp_path / f"{name}-{view}.csv").read_text(encoding="utf-8").splitlines()
        )
        shown = resp.split("```")[1].strip().splitlines()
        assert shown == file_lines or shown[-1].startswith("… ")


def test_a_board_feature_is_named_in_the_export_warning(pcb, tmp_path):
    """The exclusion is visible: the model's feature set comes out as a
    named warning rather than a silent absence."""
    model = {
        "instances": [
            {"refdes": "ARR1", "x": 0.0, "y": 0.0, "footprint": "__gen_ARR1"},
            {"refdes": "U1", "x": 1.0, "y": 1.0, "part_lcsc": "C1"},
        ],
        "nets": [],
        "board_features": {"ARR1"},
    }
    assert export.excluded_features(model) == ["ARR1"]
    warns = pcb._export_warnings(model, "bom")
    assert any("ARR1" in w and "excluded" in w for w in warns)


def test_csv_preview_announces_a_cut():
    text = "\n".join(str(n) for n in range(50))
    out = export.csv_preview(text, limit=40).splitlines()
    assert out[:40] == [str(n) for n in range(40)]
    assert out[-1] == "… 10 more line(s) in the file"
    assert export.csv_preview("a\nb", limit=40) == "a\nb"
