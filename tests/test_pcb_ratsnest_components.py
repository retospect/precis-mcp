"""Fab airwires connect copper components, preserving genuine missing fanouts."""

from __future__ import annotations

import copy
import math
import re
from typing import Any

import pytest

from precis.pcb import connectivity, gerber, gerber_view


def _pad(refdes: str, x: float, y: float, **extra: Any) -> dict[str, Any]:
    return {
        "refdes": refdes,
        "pin": "1",
        "net": "GND",
        "layer": "F.Cu",
        "shape": "circle",
        "x": x,
        "y": y,
        "w": 0.4,
        "h": 0.4,
        **extra,
    }


def _track(
    a: tuple[float, float], b: tuple[float, float], **extra: Any
) -> dict[str, Any]:
    return {
        "ctype": "track",
        "net": "GND",
        "layer": "F.Cu",
        "width_mm": 0.2,
        "segments": [{"shape": "line", "start": list(a), "end": list(b)}],
        **extra,
    }


def _model() -> dict[str, Any]:
    return {
        "layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        "outline": [[0, 0], [12, 0], [12, 8], [0, 8]],
        "pads": [_pad("PV1", 2, 2), _pad("J1", 8, 2)]
        + [_pad(refdes, x, 5) for refdes, x in (("C14", 4), ("C3", 6), ("C4", 8))],
        "copper": [
            _track((2, 2), (3, 2)),
            _track((8, 2), (9, 2)),
            *[
                {
                    "ctype": "via",
                    "net": "GND",
                    "x": x,
                    "y": 2,
                    "dia_mm": 0.6,
                    "drill_mm": 0.3,
                    "span": ["F.Cu", "B.Cu"],
                }
                for x in (3, 9)
            ],
            {
                "ctype": "pour",
                "layer": "In1.Cu",
                "net": "GND",
                "polygon": [[1, 1], [10, 1], [10, 7], [1, 7]],
            },
        ],
    }


def _manifest() -> dict[str, Any]:
    return {
        "board_ref_id": 1,
        "snapshot": "ratsnest-test",
        "instances": [],
        "hover": {"unrouted": ["GND"]},
    }


def _render(model: dict[str, Any], **kwargs: Any) -> str:
    files = gerber.export_fab(model, name="t", allow_synthesized=True)
    return gerber_view.render_fab_svg(
        files,
        connectivity_model=model,
        selection_manifest=_manifest(),
        **kwargs,
    )


def _wires(svg: str) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    lines = re.findall(r'<line\b[^>]*class="rs-line"[^>]*/>', svg)
    wires = []
    for line in lines:
        coords = {
            name: float(value)
            for name, value in re.findall(r'\b(x1|y1|x2|y2)="([^"]+)"', line)
        }
        wires.append(((coords["x1"], coords["y1"]), (coords["x2"], coords["y2"])))
    return wires


@pytest.mark.parametrize("remove_stub, expected", [(False, 3), (True, 4)])
def test_plane_connected_pv1_only_gets_an_airwire_when_its_stub_is_missing(
    remove_stub: bool, expected: int
) -> None:
    """Caught gr565255: a pending GND net drew an internal PV1 airwire."""
    model = _model()
    if remove_stub:
        model["copper"].pop(0)
    assert [(i.net, i.components) for i in connectivity.net_islands(model)] == [
        ("GND", expected + 1)
    ]
    wires = _wires(_render(model))
    assert len(wires) == expected
    if not remove_stub:
        assert not any(set(wire) == {(2, 2), (8, 2)} for wire in wires)


def test_contracting_copper_components_rebuilds_the_mst_instead_of_filtering_it() -> (
    None
):
    model = _model()
    model["pads"] = [
        _pad(refdes, x, 2) for refdes, x in zip("ABCD", (2, 3, 4, 5), strict=True)
    ]
    model["copper"] = [
        _track((2, 2), (2, 1)),
        _track((2, 1), (5, 1)),
        _track((5, 1), (5, 2)),
    ]
    wires = _wires(_render(model))
    # Filtering the original three-edge MST leaves a cycle between the
    # contracted A+D, B, C components. Exactly two shortest edges suffice.
    assert len(wires) == 2
    assert all(math.dist(*wire) == pytest.approx(1) for wire in wires)


@pytest.mark.parametrize("break_kind", ["span", "hole", "foreign"])
def test_via_spans_pour_holes_and_foreign_copper_preserve_missing_connections(
    break_kind: str,
) -> None:
    model = _model()
    if break_kind == "span":
        model["copper"][2]["span"] = ["F.Cu", "F.Cu"]
    elif break_kind == "hole":
        model["copper"][-1]["holes"] = [[[2, 1], [4, 1], [4, 3], [2, 3]]]
    else:
        model["copper"][-1]["net"] = "OTHER"
    assert len(_wires(_render(model))) == 4


@pytest.mark.parametrize("shape", ["polygon", "obround"])
def test_nonflash_pads_participate_in_missing_connections(shape: str) -> None:
    model = _model()
    extra: dict[str, Any] = {"shape": shape}
    if shape == "polygon":
        extra["poly"] = [[3.8, 4.8], [4.2, 4.8], [4.2, 5.2], [3.8, 5.2]]
    else:
        extra.update(w=0.8, h=0.4, oblique_world_rot=45)
    model["pads"][2].update(extra)
    wires = _wires(_render(model))
    assert len(wires) == 3
    assert any((4, 5) in wire for wire in wires)


def test_same_pin_physical_pads_use_the_shortest_component_endpoint() -> None:
    model = _model()
    model["pads"] = [_pad("A", 2, 2), _pad("A", 8, 2), _pad("B", 9, 2)]
    model["copper"] = []
    assert _wires(_render(model)) == [((8, 2), (9, 2))]


def test_scoped_component_wire_keeps_its_off_frame_endpoint_and_highlight() -> None:
    model = _model()
    model["pads"] = [_pad("A", 2, 2), _pad("B", 9, 2)]
    model["copper"] = []
    svg = _render(model, scope={"bbox": [1, 1, 3, 3], "nets": ["GND"]})
    assert _wires(svg) == [((2, 2), (9, 2))]
    assert 'class="rs-line pcb-hl"' in svg
    assert 'class="rs-hit" data-pcb-net="GND"' in svg


def test_all_connected_has_no_airwires_and_raw_gerber_keeps_conservative_mst() -> None:
    model = _model()
    model["pads"] = model["pads"][:2]
    assert _wires(_render(model)) == []
    files = gerber.export_fab(model, name="t", allow_synthesized=True)
    svg = gerber_view.render_fab_svg(files, selection_manifest=_manifest())
    assert len(_wires(svg)) == 1


def test_unmatched_gerber_identity_stays_disconnected() -> None:
    model = _model()
    model["pads"] = model["pads"][:2]
    files = gerber.export_fab(model, name="t", allow_synthesized=True)
    incomplete = copy.deepcopy(model)
    incomplete["pads"][0]["refdes"] = "UNKNOWN"
    svg = gerber_view.render_fab_svg(
        files,
        connectivity_model=incomplete,
        selection_manifest=_manifest(),
    )
    assert len(_wires(svg)) == 1


def test_only_pending_nets_get_airwires() -> None:
    model = _model()
    files = gerber.export_fab(model, name="t", allow_synthesized=True)
    manifest = _manifest()
    manifest["hover"]["unrouted"] = ["OTHER"]
    svg = gerber_view.render_fab_svg(
        files, connectivity_model=model, selection_manifest=manifest
    )
    assert _wires(svg) == []


def test_airwire_ties_are_deterministic_across_model_input_order() -> None:
    model = _model()
    expected = _wires(_render(model))
    model["pads"].reverse()
    model["copper"].reverse()
    assert _wires(_render(model)) == expected


def test_connectivity_is_only_queried_for_pending_nets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_model: dict[str, Any]) -> Any:
        pytest.fail("no pending nets should not need copper connectivity")

    monkeypatch.setattr(connectivity, "connected_pin_pairs", fail)
    model = _model()
    files = gerber.export_fab(model, name="t", allow_synthesized=True)
    gerber_view.render_fab_svg(files, connectivity_model=model)


def test_handler_passes_the_exact_export_model_to_connectivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.handlers.pcb import PcbHandler

    model = _model()
    handler = object.__new__(PcbHandler)
    monkeypatch.setattr(handler, "_fab_svg_model", lambda *_args: (model, []))
    monkeypatch.setattr(handler, "_fab_overlays", lambda *_args: [])
    seen: list[dict[str, Any]] = []
    original = connectivity.connected_pin_pairs

    def capture(
        actual: dict[str, Any], **kwargs: Any
    ) -> set[frozenset[tuple[str, str]]]:
        seen.append(actual)
        return original(actual, **kwargs)

    monkeypatch.setattr(connectivity, "connected_pin_pairs", capture)
    svg = handler._render_fab_svg(1, "t", selection_manifest=_manifest()).body
    assert len(seen) == 1
    assert seen[0] is model
    assert len(_wires(svg)) == 3
