"""Board-page hover/ratsnest data (gr495389 / gr495390 / gr495391): the SVG
carries part/pad/net identity per shape, failed nets' pads go red, unrouted
nets get a toggleable ratsnest, and the facts block joins catalogue rows with
``route_state`` rows. Hand-built model, no DB."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

from precis.pcb import gerber, gerber_view
from precis_web import pcb_hover

_LAYERS = ["F.Cu", "B.Cu"]


def _pad(refdes: str, pin: str, net: str, x: float, y: float) -> dict[str, Any]:
    return {
        "layer": "F.Cu",
        "net": net,
        "shape": "rect",
        "x": x,
        "y": y,
        "w": 0.6,
        "h": 0.8,
        "refdes": refdes,
        "pin": pin,
    }


def _model() -> dict[str, Any]:
    return {
        "layers": _LAYERS,
        "outline": [[0, 0], [30, 0], [30, 20], [0, 20]],
        "copper": [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "VBUS",
                "width_mm": 0.25,
                "segments": [
                    {"shape": "line", "start": [2.0, 2.0], "end": [10.0, 2.0]}
                ],
            }
        ],
        "pads": [
            _pad("J1", "1", "VBUS", 2.0, 2.0),
            _pad("C2", "1", "VBUS", 10.0, 2.0),
            _pad("J1", "5", "CC2", 2.0, 10.0),
            _pad("R9", "1", "CC2", 12.0, 10.0),
            _pad("R9", "2", "CC2", 20.0, 14.0),
        ],
        "silkscreen": {"top": [], "bottom": []},
    }


_HOVER = {
    "facts": {
        "parts": {"C2": {"value": "10uF", "package": "0603", "rating": "6.3V"}},
        "nets": {"CC2": {"pins": ["J1.5", "R9.1", "R9.2"], "status": "failed"}},
    },
    "unrouted": ["CC2"],
    "failed": ["CC2"],
}


def _svg(hover: dict[str, Any] | None = _HOVER) -> ET.Element:
    files = gerber.export_fab(_model(), name="t", allow_synthesized=True)
    manifest = (
        None
        if hover is None
        else {"board_ref_id": 1, "snapshot": "tok", "instances": [], "hover": hover}
    )
    return ET.fromstring(
        gerber_view.render_fab_svg(files, title="t", selection_manifest=manifest)
    )


def _attrs(root: ET.Element, key: str) -> list[ET.Element]:
    return [e for e in root.iter() if key in e.attrib]


def test_pad_track_and_net_shapes_carry_identity() -> None:
    root = _svg(None)
    pads = {
        (e.get("data-pcb-part"), e.get("data-pcb-pad"), e.get("data-pcb-net"))
        for e in _attrs(root, "data-pcb-pad")
    }
    assert {("C2", "1", "VBUS"), ("J1", "5", "CC2"), ("R9", "2", "CC2")} <= pads
    tracks = [
        e
        for e in _attrs(root, "data-pcb-net")
        if e.tag.endswith("path") and "data-pcb-pad" not in e.attrib
    ]
    assert tracks and {e.get("data-pcb-net") for e in tracks} == {"VBUS"}


def test_ratsnest_and_failed_pads_only_with_route_facts() -> None:
    plain = _svg(None)
    assert not any(e.get("id") == "ratsnest" for e in plain.iter())
    assert not _attrs(plain, "data-pcb-failed")
    root = _svg()
    lines = _attrs(root, "data-pcb-ratsnest")
    # MST over three CC2 pads = two edges, each a visible + a hover line
    assert len(lines) == 4 and {e.get("data-pcb-net") for e in lines} == {"CC2"}
    failed = _attrs(root, "data-pcb-failed")
    assert {e.get("data-pcb-net") for e in failed} == {"CC2"}
    assert all("pcb-failed" in (e.get("class") or "") for e in failed)
    # the routed net is neither red nor in the ratsnest
    assert not any(e.get("data-pcb-net") == "VBUS" for e in failed + lines)


def test_ratsnest_has_a_legend_toggle_and_the_facts_ride_in_metadata() -> None:
    root = _svg()
    row = next(e for e in root.iter() if e.get("data-layer") == "ratsnest")
    assert "rs-off" in row.get("onclick", "")
    style = next(e for e in root.iter() if e.tag.endswith("style")).text or ""
    assert ".rs-off .ratsnest" in style and "svg:not(.rs-off) .pcb-failed" in style
    meta = next(e for e in root.iter() if e.get("id") == "pcb-facts")
    assert json.loads(meta.text or "")["nets"]["CC2"]["status"] == "failed"
    assert not any(e.get("data-layer") == "ratsnest" for e in _svg(None).iter())


_TABLES: dict[str, list[dict[str, Any]]] = {
    "pcb_instances": [
        {"instance_id": 1, "refdes": "C2", "component_id": 10},
        {"instance_id": 2, "refdes": "J1", "component_id": 20},
        {"instance_id": 3, "refdes": "R9", "component_id": 30},
    ],
    "pcb_components": [
        {"component_id": 10, "part_lcsc": "C19702", "footprint": "C0603", "label": ""},
        {"component_id": 20, "part_lcsc": None, "footprint": "USB-C", "label": "USB"},
        {"component_id": 30, "part_lcsc": "C1", "footprint": "R0603", "label": ""},
    ],
    "parts": [
        {
            "lcsc": "C19702",
            "description": "10uF 6.3V X5R capacitor MLCC",
            "package": "0603",
            "mfr_part": "CL10A106",
            "datasheet_url": "https://datasheet.lcsc.com/lcsc/C19702.pdf",
        },
        {
            "lcsc": "C1",
            "description": "10kΩ ±1% 100mW resistor",
            "package": "0402",
            "mfr_part": "R",
        },
    ],
    "pcb_pins": [
        {"pin_id": 1, "component_id": 10, "pad": "1", "name": "P"},
        {"pin_id": 2, "component_id": 20, "pad": "5", "name": "CC2"},
        {"pin_id": 3, "component_id": 30, "pad": "1", "name": "1"},
    ],
    "pcb_nets": [
        {"net_id": 7, "name": "VBUS", "net_class": "power", "est_current_a": 0.5},
        {"net_id": 8, "name": "CC2", "est_current_a": None},
    ],
    "pcb_netconns": [
        {"net_id": 7, "instance_id": 1, "pin_id": 1},
        {"net_id": 8, "instance_id": 2, "pin_id": 2},
        {"net_id": 8, "instance_id": 3, "pin_id": 3},
    ],
}


def test_facts_join_catalogue_rows_and_route_state() -> None:
    route: list[dict[str, Any]] = [
        {"name": "VBUS", "status": "realized", "note": None},
        {"name": "CC2", "status": "failed", "note": "no path on F.Cu"},
    ]
    out = pcb_hover.facts_from_tables(_TABLES, route)
    parts = out["facts"]["parts"]
    assert parts["C2"] == {
        "value": "10uF",
        "package": "0603",
        "rating": "6.3V",
        "lcsc": "C19702",
        "datasheet": "https://datasheet.lcsc.com/lcsc/C19702.pdf",
        "script": None,
    }
    assert parts["R9"]["value"] == "10k" and parts["R9"]["rating"] == "0.1W"
    assert parts["J1"] == {
        "value": "USB",
        "package": "USB-C",
        "rating": None,
        "lcsc": None,
        "datasheet": None,
        "script": None,
    }
    nets = out["facts"]["nets"]
    assert nets["VBUS"]["current_a"] == 0.5 and nets["VBUS"]["reason"] is None
    assert nets["VBUS"]["net_class"] == "power" and nets["CC2"]["net_class"] is None
    assert nets["CC2"]["pins"] == ["J1.5", "R9.1"]
    assert nets["CC2"]["reason"] == "no path on F.Cu"  # carried verbatim
    assert out["unrouted"] == ["CC2"] and out["failed"] == ["CC2"]


def test_part_summary_shows_a_readable_package_never_an_easyeda_id() -> None:
    """gr520540: an opaque footprint snapshot (the EasyEDA uuid) is not a
    package name — the catalog ``package`` wins, a readable footprint
    snapshot still shows, and an opaque one leaves ``package`` unset."""
    s = pcb_hover.part_summary(
        {"package": "0402"}, {"footprint": "c0f1e49edd994701b28afed5ddf5e652"}
    )
    assert s["package"] == "0402"
    s = pcb_hover.part_summary(None, {"footprint": "QFN-40-EP(5x5)"})
    assert s["package"] == "QFN-40-EP(5x5)"
    for opaque in (
        "c0f1e49edd994701b28afed5ddf5e652",
        "easyeda:c0f1e49edd994701b28afed5ddf5e652",
    ):
        assert pcb_hover.part_summary(None, {"footprint": opaque})["package"] is None


def test_unreasoned_failure_and_unrouted_net_still_say_something() -> None:
    route: list[dict[str, Any]] = [
        {"name": "VBUS", "status": "unrouted", "note": None},
        {"name": "CC2", "status": "failed", "note": None},
    ]
    out = pcb_hover.facts_from_tables(_TABLES, route)
    assert out["facts"]["nets"]["CC2"]["reason"] == "router reported no reason"
    assert out["unrouted"] == ["CC2", "VBUS"] and out["failed"] == ["CC2"]


def test_facts_merge_the_traced_placement_line_per_refdes() -> None:
    """gr520540: the Selection box's board-script link is a facts field —
    ``scripts`` merges ``{file, line, ws}`` per refdes, ``None`` where the
    trace knows no maker."""
    scripts = {
        "C2": {"file": "main.py", "line": 4, "ws": "leaf-a"},
        "J1": None,
    }
    out = pcb_hover.facts_from_tables(_TABLES, [], scripts=scripts)
    parts = out["facts"]["parts"]
    assert parts["C2"]["script"] == {"file": "main.py", "line": 4, "ws": "leaf-a"}
    assert parts["J1"]["script"] is None
    assert parts["R9"]["script"] is None


_E2E_SCRIPT = """\
import precis_geom.pcb as pcb


def build():
    b = pcb.Board("e2e")
    u1 = b.part("U1", pins=["1"])
    u2 = b.part("U2", pins=["1"])
    b.wire(u1["1"], u2["1"], name="VCC")
    return b
"""


def test_placed_scripts_names_the_maker_frames_script_line(
    store: Any, tmp_path: Any
) -> None:
    """gr520540: the trace's ``meta.frame`` stamps resolve to the deepest
    user frame — the script line that placed the part — never recomputed
    (same data ``view='frames'`` walks)."""
    from precis.dispatch import Hub
    from precis.handlers.pcb import PcbHandler
    from precis.store import Store
    from precis_geom import trace, trace_schema
    from precis_web import pcb_design
    from tests.workspaces._script import load_script

    assert isinstance(store, Store)
    mod = load_script(tmp_path, _E2E_SCRIPT)
    with trace.run(work_root=tmp_path) as tr:
        board = mod.build()
        doc = tr.dump()
    doc["run"]["workspace"] = "leaf-a"
    h = PcbHandler(hub=Hub(store=store))
    h.put(id="tb", args={**board.to_batch(), "trace": trace_schema.dumps(doc)})
    ref = store.get_ref(kind="pcb", id="tb")
    assert ref is not None
    with store.pcb_view_tx() as conn:
        view = pcb_design.capture_view(store, ref, conn=conn)
        placed = pcb_hover.placed_scripts(store, view)
    u1 = placed["U1"]
    assert u1 is not None
    assert u1["file"] == "main.py" and u1["line"] == 4
    assert u1["ws"] == "leaf-a"


def test_placed_scripts_is_empty_without_a_bound_trace(store: Any) -> None:
    """A board with no construction trace yields no script facts — the
    Selection box shows no link (gr520540's "when that is known")."""
    from precis.dispatch import Hub
    from precis.handlers.pcb import PcbHandler
    from precis.store import Store
    from precis_web import pcb_design

    assert isinstance(store, Store)
    h = PcbHandler(hub=Hub(store=store))
    h.put(
        id="plain",
        args={
            "components": [{"refdes": "R1", "pins": [{"name": "1"}]}],
            "nets": [{"name": "VCC"}],
            "connections": [{"net": "VCC", "refdes": "R1", "pin": "1"}],
        },
    )
    ref = store.get_ref(kind="pcb", id="plain")
    assert ref is not None
    with store.pcb_view_tx() as conn:
        view = pcb_design.capture_view(store, ref, conn=conn)
        assert pcb_hover.placed_scripts(store, view) == {}
