"""Board-page hover/ratsnest data (gr495389 / gr495390 / gr495391): the SVG
carries part/pad/net identity per shape, failed nets' pads go red, unrouted
nets get a toggleable ratsnest, and the facts block joins catalogue rows with
``route_state`` rows. Hand-built model, no DB."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import pytest

from precis.pcb import connectivity, gerber, gerber_view
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
    # the page-side master toggle (pcb-workbench.js) syncs this row by id
    assert row.get("id") == "legend-ratsnest"
    style = next(e for e in root.iter() if e.tag.endswith("style")).text or ""
    assert ".rs-off .ratsnest" in style and "svg:not(.rs-off) .pcb-failed" in style
    meta = next(e for e in root.iter() if e.get("id") == "pcb-facts")
    assert json.loads(meta.text or "")["nets"]["CC2"]["status"] == "failed"
    assert not any(e.get("data-layer") == "ratsnest" for e in _svg(None).iter())


def test_ratsnest_paints_under_the_copper_layers() -> None:
    """gr531205: the airwire group is emitted BEFORE the layer stack, so a
    realized 0.15mm track reads above the overlay instead of under it."""
    root = _svg()
    board = next(e for e in root.iter() if "transform" in e.attrib)
    ids = [g.get("id") or "" for g in board if g.tag.endswith("g")]
    first_layer = next(i for i, gid in enumerate(ids) if gid.startswith("layer-"))
    assert 0 < ids.index("ratsnest") < first_layer


def test_ratsnest_lines_carry_isolate_hooks_and_density_alpha() -> None:
    """gr531205: the visible wire (rs-line) is density-faded and distinct
    from the wide hover wire (rs-hit); the group reports the unrouted
    count the alpha came from."""
    root = _svg()
    group = next(e for e in root.iter() if e.get("id") == "ratsnest")
    assert group.get("data-pcb-unrouted") == "1"
    lines = _attrs(root, "data-pcb-ratsnest")
    by_class = {e.get("class") for e in lines}
    assert by_class == {"rs-line", "rs-hit"}
    visible = [e for e in lines if e.get("class") == "rs-line"]
    assert visible and all(e.get("stroke-opacity") == "1.0" for e in visible)
    hits = [e for e in lines if e.get("class") == "rs-hit"]
    assert hits and all(e.get("stroke-opacity") == "0.01" for e in hits)


def test_ratsnest_alpha_fades_with_the_unrouted_count() -> None:
    """gr531205: full alpha through 10 unrouted nets, a linear ramp to the
    0.25 floor at 40, the floor past it — sparse boards stay readable,
    dense ones stop glowing."""
    a = gerber_view._ratsnest_alpha
    assert a(1) == 1.0 and a(10) == 1.0
    assert a(25) == 0.625
    assert a(40) == 0.25 and a(400) == 0.25


def _partly_connected_model() -> dict[str, Any]:
    """A thin stub and via join two GND pads; the F.Cu pad above B.Cu is orphaned."""
    model = _model()
    model["pads"] = [
        _pad("C1", "2", "GND", 2.0, 2.0),
        {**_pad("C2", "2", "GND", 10.0, 2.0), "layer": "B.Cu"},
        {
            **_pad("C3", "2", "GND", 8.0, 2.0),
            "shape": "polygon",
            "poly": [(7.6, 1.6), (8.4, 1.6), (8.4, 2.4), (7.6, 2.4)],
        },
    ]
    model["copper"] = [
        {
            "ctype": "track",
            "layer": "F.Cu",
            "net": "GND",
            "width_mm": 0.15,
            "segments": [{"shape": "line", "start": [2, 2], "end": [2.5, 2]}],
        },
        {
            "ctype": "via",
            "net": "GND",
            "x": 2.5,
            "y": 2.0,
            "dia_mm": 0.6,
            "drill_mm": 0.3,
            "span": _LAYERS,
        },
        {
            "ctype": "track",
            "layer": "B.Cu",
            "net": "GND",
            "width_mm": 0.15,
            "segments": [{"shape": "line", "start": [2.5, 2], "end": [10, 2]}],
        },
    ]
    return model


def _pending_airwires(
    model: dict[str, Any], connectivity_model: dict[str, Any] | None
) -> list[ET.Element]:
    files = gerber.export_fab(model, name="t", allow_synthesized=True)
    root = ET.fromstring(
        gerber_view.render_fab_svg(
            files,
            selection_manifest={
                "board_ref_id": 1,
                "snapshot": "tok",
                "instances": [],
                "hover": {"unrouted": ["GND"]},
            },
            connectivity_model=connectivity_model,
        )
    )
    return [e for e in root.iter() if e.get("class") == "rs-line"]


@pytest.mark.parametrize("shape", ["rect", "polygon"])
def test_ratsnest_joins_copper_components_at_the_closest_pad_pair(shape: str) -> None:
    """gr565255: filtering an ordinary pad MST still leaves two orphan links."""
    model = _partly_connected_model()
    model["pads"][2]["shape"] = shape
    lines = _pending_airwires(model, model)
    assert len(lines) == 1
    assert lines[0].get("data-pcb-net") == "GND"
    endpoints = {
        (float(lines[0].get(f"x{i}", "nan")), float(lines[0].get(f"y{i}", "nan")))
        for i in (1, 2)
    }
    assert endpoints == {(10.0, 2.0), (8.0, 2.0)}


def test_fully_connected_pending_net_has_no_airwires() -> None:
    model = _partly_connected_model()
    model["pads"][2]["layer"] = "B.Cu"
    assert _pending_airwires(model, model) == []


def test_ratsnest_joins_three_components_with_two_airwires() -> None:
    model = _partly_connected_model()
    model["pads"].append(_pad("C4", "2", "GND", 18.0, 2.0))
    lines = _pending_airwires(model, model)
    assert len(lines) == 2
    endpoints = {
        frozenset(float(line.get(f"x{i}", "nan")) for i in (1, 2)) for line in lines
    }
    assert endpoints == {frozenset((10.0, 8.0)), frozenset((10.0, 18.0))}


@pytest.mark.parametrize("connectivity_model", [None, {}])
def test_ratsnest_without_connectivity_keeps_the_full_pad_tree(
    connectivity_model: dict[str, Any] | None,
) -> None:
    lines = _pending_airwires(_partly_connected_model(), connectivity_model)
    assert len(lines) == 2
    assert {e.get("data-pcb-net") for e in lines} == {"GND"}


def test_ratsnest_keeps_airwires_for_pads_missing_from_connectivity() -> None:
    model = _partly_connected_model()
    partial = {**model, "pads": model["pads"][:2]}
    assert len(_pending_airwires(model, partial)) == 1


@pytest.mark.parametrize("pour", [None, "declared", "stored"])
def test_pending_pin_components_match_unfiltered_connectivity(pour: str | None) -> None:
    model = _partly_connected_model()
    if pour == "declared":
        model["pending_pours"] = [{"net": "GND", "layer": "F.Cu"}]
    elif pour == "stored":
        model["copper"].append(
            {
                "ctype": "pour",
                "net": "GND",
                "layer": "F.Cu",
                "polygon": model["outline"],
            }
        )
    unrelated = _model()
    model["pads"] = unrelated["pads"] + model["pads"]
    model["copper"] = unrelated["copper"] + model["copper"]
    full = connectivity.pin_components(model)
    scoped = connectivity.pin_components(model, nets={"GND"})
    assert full is not None and scoped is not None
    assert ("J1", "1") in full  # Default extraction still includes routed nets.
    assert set(scoped) == {("C1", "2"), ("C2", "2"), ("C3", "2")}
    assert scoped[("C1", "2")] == scoped[("C2", "2")]
    assert (scoped[("C1", "2")] == scoped[("C3", "2")]) == (pour is not None)
    for a in scoped:
        for b in scoped:
            assert (scoped[a] == scoped[b]) == (full[a] == full[b])
    assert connectivity.pin_components(model, nets=set()) is None


def test_pending_connectivity_does_not_examine_unrelated_routed_segments() -> None:
    """gr565255: even primitive extraction must skip a large routed net."""

    class UnexaminedSegments(list[dict[str, Any]]):
        def __iter__(self) -> Iterator[dict[str, Any]]:
            raise AssertionError("unrelated routed segments were examined")

    model = _partly_connected_model()
    unrelated = _model()["copper"][0]
    unrelated["segments"] = UnexaminedSegments(unrelated["segments"] * 5000)
    connectivity_model = {**model, "copper": [unrelated, *model["copper"]]}
    components = connectivity.pin_components(connectivity_model, nets={"GND"})
    assert components is not None
    assert set(components) == {("C1", "2"), ("C2", "2"), ("C3", "2")}
    # Export normal artwork; only the viewer's connectivity input contains
    # the guarded segments, so this exercises render_fab_svg's net scope.
    assert len(_pending_airwires(model, connectivity_model)) == 1


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
    assert nets["CC2"]["current_a"] is None  # unset → no current fact (gr520542)
    assert nets["VBUS"]["net_class"] == "power" and nets["CC2"]["net_class"] is None
    assert nets["CC2"]["pins"] == ["J1.5", "R9.1"]
    assert nets["CC2"]["reason"] == "no path on F.Cu"  # carried verbatim
    assert out["unrouted"] == ["CC2"] and out["failed"] == ["CC2"]


def test_facts_net_current_is_the_stored_amps_or_nothing() -> None:
    """gr520542: ``current_a`` is the stored amps figure verbatim — None
    only when the net carries no annotation; an explicit 0 (a real
    annotation) survives instead of reading as unset."""
    tables = {
        "pcb_nets": [
            {"net_id": 1, "name": "SIG", "est_current_a": None},
            {"net_id": 2, "name": "OFF", "est_current_a": 0},
            {"net_id": 3, "name": "LED", "est_current_a": 0.02},
        ],
    }
    nets = pcb_hover.facts_from_tables(tables, [])["facts"]["nets"]
    assert nets["SIG"]["current_a"] is None
    assert nets["OFF"]["current_a"] == 0
    assert nets["LED"]["current_a"] == 0.02


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
    u1["1"].etype("bidi", fmax_hz=0.0, assume="fixture pin")
    u2["1"].etype("bidi", fmax_hz=0.0, assume="fixture pin")
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


def test_oversized_pending_net_skips_component_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A huge pending net keeps the full MST instead of stalling the request."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("component extraction must be skipped")

    model = _partly_connected_model()
    scoped = len(_pending_airwires(model, model))
    monkeypatch.setattr(connectivity, "pin_components", refuse)
    monkeypatch.setattr(gerber_view, "_RATSNEST_COMPONENT_MAX_COPPER", -1)
    assert len(_pending_airwires(model, model)) > scoped
