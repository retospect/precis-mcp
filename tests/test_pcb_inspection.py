"""gr540834: text-first geometry, topology, filtering and bounded readback."""

from __future__ import annotations

import ast
import math
from typing import Any

import pytest
from shapely.geometry import box

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.pcb import PcbHandler
from precis.pcb import drc, inspection
from precis.store import Store


def _track(
    a: tuple[float, float], b: tuple[float, float], layer: str = "F.Cu"
) -> dict[str, Any]:
    return {
        "ctype": "track",
        "net": "SIG",
        "layer": layer,
        "width_mm": 0.2,
        "segments": [{"shape": "line", "start": a, "end": b}],
    }


def _pad(rd: str, x: float, y: float, layer: str = "F.Cu") -> dict[str, Any]:
    return {
        "refdes": rd,
        "pin": "1",
        "x": x,
        "y": y,
        "layer": layer,
        "net": "SIG",
        "shape": "circle",
        "w": 0.5,
    }


def _data() -> inspection.Inspection:
    model = {
        "layers": ["F.Cu", "B.Cu"],
        "pads": [_pad("U1", 0, 0), _pad("U2", 10, 5, "B.Cu"), _pad("U3", 5, 4)],
        "copper": [
            _track((0, 0), (10, 0)),
            _track((5, 0), (5, 4)),
            _track((10, 0), (10, 5), "B.Cu"),
            {
                "ctype": "via",
                "net": "SIG",
                "x": 10,
                "y": 0,
                "dia_mm": 0.6,
                "drill_mm": 0.3,
                "layers": ["F.Cu", "B.Cu"],
            },
        ],
        "silk_census": [
            {
                "refdes": "U1",
                "kind": "refdes",
                "side": "top",
                "outcome": "relocated",
                "x_mm": 0.8,
                "y_mm": 1.2,
                "reason": "neighbor clearance",
            },
        ],
        "silkscreen": {
            "top": [
                {
                    "role": "refdes",
                    "refdes": "U1",
                    "width_mm": 0.15,
                    "segments": [{"start": (0.8, 1.2), "end": (1.5, 1.2)}],
                },
                {
                    "role": "refdes",
                    "refdes": "U1",
                    "width_mm": 0.15,
                    "segments": [{"start": (0.8, 1.2), "end": (0.8, 2)}],
                },
            ]
        },
    }
    graph = {
        "nets": [{"name": "SIG", "net_class": "sense"}],
        "instances": [
            {
                "refdes": "U1",
                "x": 0,
                "y": 0,
                "rot": 90,
                "layer": "top",
                "label_spot": "top-right",
            },
            {"refdes": "U2", "x": 10, "y": 5, "layer": "bottom"},
            {"refdes": "U3", "x": 5, "y": 4, "layer": "top"},
        ],
    }
    findings = [
        drc.DrcFinding(
            "clearance",
            "error",
            "U1.1",
            "test witness",
            objects=({"refdes": "U1", "x": 0, "y": 0},),
        )
    ]
    return inspection.Inspection(
        model,
        graph,
        {"U1": box(-1, -1, 1, 1), "U2": box(9, 4, 11, 6), "U3": box(4, 3, 6, 5)},
        findings,
    )


def test_wire_nodes_t_junction_and_via_with_cumulative_distance() -> None:
    body = "\n".join(_data().wire({"pin": "U1.1"}))
    assert "branch F.Cu (5.00,0.00) cum=5.00" in body
    assert "via (10.00,0.00) layers=F.Cu,B.Cu" in body
    assert "endpoint U2.1 cum=15.00 B.Cu (10.00,5.00)" in body
    assert "endpoint U3.1 cum=9.00" in body


def test_wire_filter_does_not_reset_distance_or_remove_via() -> None:
    body = "\n".join(_data().wire({"pin": "U1.1", "layer": "B.Cu", "min_length": 1}))
    assert "segment B.Cu len=5.00 cum=15.00" in body
    assert "segment F.Cu" not in body
    assert "via (10.00,0.00)" in body
    with pytest.raises(NotFound):
        _data().wire({"net": "SIG", "net_class": "rf"})


def test_crossing_on_other_layer_is_not_a_junction() -> None:
    data = _data()
    data.model["copper"] = [_track((0, 0), (10, 0)), _track((5, -5), (5, 5), "B.Cu")]
    data = inspection.Inspection(data.model, data.graph, data.courtyards, [])
    body = "\n".join(data.wire({"pin": "U1.1"}))
    assert "segment B.Cu" not in body
    assert "disconnected copper:" in body


def test_thick_copper_and_off_center_pad_contacts_are_connected() -> None:
    data = _data()
    data.model["pads"] = [_pad("U1", 0, 0.3), _pad("U2", 10, 0.3)]
    data.model["copper"] = [
        _track((0, 0), (5, 0)),
        _track((5, 0.15), (10, 0.15)),
    ]
    data = inspection.Inspection(data.model, data.graph, {}, [])
    body = "\n".join(data.wire({"pin": "U1.1"}))
    assert "copper contact" in body
    assert "endpoint U2.1 cum=10.00" in body
    assert "disconnected copper" not in body


def test_physical_pad_number_resolves_named_pin() -> None:
    data = _data()
    data.model["pads"][0]["pin"] = "SENSE"
    data.graph["nets"][0]["members"] = [{"refdes": "U1", "pin": "SENSE", "pad": "3"}]
    data = inspection.Inspection(data.model, data.graph, data.courtyards, [])
    body = "\n".join(data.wire({"pin": "U1.3"}))
    assert "pin U1.SENSE" in body
    assert "endpoint U2.1" in body


def test_imported_arc_and_pinless_copper_are_walkable() -> None:
    data = _data()
    data.model["pads"] = []
    data.model["copper"] = [
        {
            "ctype": "track",
            "net": "SIG",
            "layer": "F.Cu",
            "width_mm": 0.2,
            "segments": [
                {
                    "shape": "arc",
                    "start": (1, 0),
                    "end": (0, 1),
                    "center": (0, 0),
                    "cw": False,
                }
            ],
        }
    ]
    data = inspection.Inspection(data.model, data.graph, {}, [])
    body = "\n".join(data.wire({"net": "SIG"}))
    assert "(no pin)" in body and "segment F.Cu" in body
    lengths = [
        float(line.split("len=")[1].split()[0])
        for line in body.splitlines()
        if "len=" in line
    ]
    assert sum(lengths) == pytest.approx(math.pi / 2, abs=0.08)


def test_part_card_reports_pads_actual_label_gap_and_findings() -> None:
    body = "\n".join(_data().part({"refdes": "U1", "side": "top", "net": "S*"}))
    assert "rot=90.00" in body
    assert "pad U1.1 F.Cu (0.00,0.00) net=SIG" in body
    assert "courtyard bbox (-1.00,-1.00)..(1.00,1.00)" in body
    assert "neighbor U3 courtyard gap=3.61" in body
    assert "label relocated spot=top-right (0.80,1.20) offset=(0.80,1.20)" in body
    assert "finding error clearance" in body
    assert "neighbor U2" not in body


def test_layer_counts_text_groups_and_density_of_crossing_track() -> None:
    data = _data()
    silk = "\n".join(data.layer({"layer": "F.Silkscreen"}))
    assert "'text': 1" in silk
    body = "\n".join(
        data.layer({"layer": "F.Cu", "bbox": [2, -0.1, 4, 0.1], "net_class": "sense"})
    )
    assert "'track': 1" in body
    assert '"SIG": 1' in body
    assert "############" in body


def test_snippet_intersects_tracks_not_just_endpoints_and_has_census() -> None:
    body = "\n".join(_data().snippet({"bbox": [2, -0.5, 4, 0.5]}))
    assert "track track F.Cu net=SIG" in body
    assert "(0.00,0.00)->(10.00,0.00)" in body
    body = "\n".join(_data().snippet({"refdes": ["U1"], "margin": 1}))
    assert "snippet bbox (-2.00,-2.00)..(2.00,2.00)" in body
    assert "silk census U1 refdes top: relocated (0.80,1.20)" in body
    assert "finding error clearance" in body


def test_pagination_is_utf8_bounded_and_next_preserves_filters() -> None:
    lines = [f"pad {i} " + "\u00e9" * 80 for i in range(100)]
    args: dict[str, Any] = {"bbox": [0, 0, 5, 5], "net": "S*"}
    seen: list[str] = []
    while True:
        body = inspection.page(lines, "board", "snippet", args)
        assert len(body.encode("utf-8")) <= 3900
        seen.extend(line for line in body.splitlines() if line.startswith("pad "))
        if "\nnext:" not in body:
            break
        args = ast.literal_eval(body.split("args=", 1)[1].removesuffix(")"))
        assert args["bbox"] == [0, 0, 5, 5] and args["net"] == "S*"
    assert seen == lines


def test_long_row_continues_without_dropping_text() -> None:
    original = "witness " + "\U0001f600" * 2000
    args: dict[str, Any] = {}
    seen: list[str] = []
    while True:
        body = inspection.page([original], "board", "part", args)
        assert len(body.encode("utf-8")) <= 3900
        seen.extend(
            line.removeprefix("  continued: ")
            for line in body.splitlines()[1:]
            if not line.startswith("next:")
        )
        if "\nnext:" not in body:
            break
        args = ast.literal_eval(body.split("args=", 1)[1].removesuffix(")"))
    assert "".join(seen) == original


@pytest.mark.parametrize(
    ("view", "args"),
    [
        ("wire", {}),
        ("wire", {"net": "SIG", "min_length": -1}),
        ("wire", {"net": "SIG", "min_length": 10**400}),
        ("wire", {"net": "SIG", "offset": 1.5}),
        ("wire", {"net": "SIG", "foo": True}),
        ("part", {"side": "rear"}),
        ("layer", {"layer": "bad"}),
        ("snippet", {"bbox": [0, 0, 1, float("nan")]}),
        ("snippet", {"refdes": "U1", "bbox": [0, 0, 1, 1]}),
        ("snippet", {}),
    ],
)
def test_inspection_misuse_has_a_next_step(view: str, args: dict[str, Any]) -> None:
    with pytest.raises(BadInput) as caught:
        inspection.render(_data(), "b", view, args)
    assert caught.value.next


def test_handler_exposes_all_inspection_views_without_recording_drc(
    store: Store,
) -> None:
    handler = PcbHandler(hub=Hub(store=store))
    handler.put(
        id="inspect",
        args={
            "components": [{"refdes": "U1", "pins": [{"name": "1"}]}],
            "nets": [{"name": "SIG", "class": "sense", "current": 0.001}],
            "connections": [{"refdes": "U1", "pin": "1", "net": "SIG"}],
        },
    )
    for view, args in (
        ("part", {"refdes": "U1"}),
        ("layer", {"layer": "F.Cu"}),
        ("snippet", {"bbox": [0, 0, 2, 2]}),
        ("wire", {"net": "SIG"}),
    ):
        body = handler.get(id="inspect", view=view, args=args).body
        assert f"inspect {view} | mm" in body
    assert "inspect part | mm" in handler.get(id="inspect#U1", view="part").body
    assert "inspect wire | mm" in handler.get(id="inspect@SIG", view="wire").body
    with pytest.raises(BadInput, match="disagrees"):
        handler.get(id="inspect#U1", view="part", args={"refdes": "U2"})
    handler.put(
        id="inspect",
        args={
            "op": "move",
            "refdes": "U1",
            "x": 10,
            "y": 10,
            "label_spot": "top-right",
        },
    )
    assert "spot=top-right" in handler.get(id="inspect#U1", view="part").body
    with store.pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM pcb_drc_runs").fetchone()
        assert row is not None and row[0] == 0
