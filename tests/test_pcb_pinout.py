"""Actual pad inspection: independent coordinate oracle and read-only DB proof."""

from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.pcb import PcbHandler
from precis.pcb import eyes

_CENTERS = [(-2, 1), (0, 1), (3, 1), (-2, -1), (0, -1), (3, -2), (4, -2)]
_NUMBERS = ["1", "2", "3", "4", "5", "6", "6"]
_NAMES = ["VCC", "CLK", "DATA", "GND", "NC"]
_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def _footprint() -> dict[str, Any]:
    return {
        "pads": [
            {
                "number": n,
                "x": x,
                "y": y,
                "shape": "RECT",
                "w": 0.6,
                "h": 0.8,
                "layer": "F.Cu",
            }
            for n, (x, y) in zip(_NUMBERS, _CENTERS, strict=True)
        ],
        "pin_map": {str(i): {"name": name} for i, name in enumerate(_NAMES, 1)},
    }


def _pins() -> list[dict[str, Any]]:
    return [
        {"pin": name, "pad": str(i), "net": None if name == "NC" else name}
        for i, name in enumerate(_NAMES, 1)
    ]


@pytest.mark.parametrize(
    ("pose", "expected"),
    [
        (
            {"x": 10, "y": 20, "rot": 0, "layer": "top"},
            [(8, 21), (10, 21), (13, 21), (8, 19), (10, 19), (13, 18), (14, 18)],
        ),
        (
            {"x": 30, "y": 40, "rot": 90, "layer": "bottom"},
            [(31, 38), (31, 40), (31, 43), (29, 38), (29, 40), (28, 43), (28, 44)],
        ),
    ],
)
def test_asymmetric_physical_rows_and_mapping(pose, expected):
    fp, pins = _footprint(), _pins()
    before = copy.deepcopy((pose, fp, pins))
    result = eyes.pinout(pose, fp, pins, _LAYERS)
    rows = result["rows"]
    assert [(r["board_x_mm"], r["board_y_mm"]) for r in rows] == pytest.approx(expected)
    assert [(r["local_x_mm"], r["local_y_mm"]) for r in rows] == _CENTERS
    assert [r["pad_number"] for r in rows] == _NUMBERS
    assert [r["mapping_state"] for r in rows] == ["connected"] * 4 + [
        "unconnected",
        "unclaimed",
        "unclaimed",
    ]
    assert rows[5]["duplicate_indices"] == rows[6]["duplicate_indices"] == [6, 7]
    assert rows[0]["mapping_sources"] == [
        {
            "pin": "VCC",
            "nets": ["VCC"],
            "sources": ["explicit-pin-pad", "footprint-pin-map"],
        }
    ]
    assert all(
        r["board_layers"] == ["B.Cu" if pose["layer"] == "bottom" else "F.Cu"]
        for r in rows
    )
    assert not result["unmatched"]
    assert (pose, fp, pins) == before


@pytest.mark.parametrize(
    ("side", "rot", "point"), [("top", 90, (11, 22)), ("bottom", 0, (12, 21))]
)
def test_extra_rotation_oracles(side, rot, point):
    row = eyes.pinout(
        {"x": 10, "y": 20, "layer": side, "rot": rot}, _footprint(), [], _LAYERS
    )["rows"][0]
    assert (row["board_x_mm"], row["board_y_mm"]) == pytest.approx(point)


@pytest.mark.parametrize("axis_value", [None, float("nan"), float("inf"), False, "bad"])
def test_unplaced_keeps_local_geometry_without_default_origin(axis_value):
    result = eyes.pinout({"x": axis_value, "y": 0}, _footprint(), _pins(), _LAYERS)
    assert not result["placed"]
    assert all(
        r["board_x_mm"] is None and r["board_y_mm"] is None for r in result["rows"]
    )
    assert result["rows"][0]["local_x_mm"] == -2
    assert result["rows"][0]["mapping_state"] == "connected"


def test_real_origin_and_through_hole_are_one_physical_row():
    fp = {"pads": [{"number": "1", "x": 1, "y": 2, "drill": 0.8}]}
    result = eyes.pinout({"x": 0, "y": 0}, fp, [{"pin": "1", "net": None}], _LAYERS)
    assert result["placed"]
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert (row["board_x_mm"], row["board_y_mm"]) == (1, 2)
    assert row["board_layers"] == _LAYERS
    assert row["mapping_sources"][0]["sources"] == ["pad-number-identity"]
    assert row["mapping_state"] == "unconnected"


def test_conflicting_pins_and_all_net_memberships_are_evidence():
    pins = _pins() + [
        {"pin": "OTHER", "pad": "2", "net": "OTHER_NET"},
        {"pin": "CLK", "pad": "2", "net": "SECOND_CLK"},
    ]
    row = eyes.pinout({"x": 0, "y": 0}, _footprint(), pins, _LAYERS)["rows"][1]
    assert row["mapping_state"] == "ambiguous"
    assert row["pin_names"] == ["CLK", "OTHER"]
    assert row["net_names"] == ["CLK", "OTHER_NET", "SECOND_CLK"]
    assert row["mapping_sources"][0]["nets"] == ["CLK", "SECOND_CLK"]
    assert "explicit OTHER disagrees with footprint name CLK" in row["notes"]


def test_explicit_pad_disagrees_with_footprint_and_missing_pad_is_unmatched():
    pins: list[dict[str, Any]] = [
        {"pin": "VCC", "pad": "99", "net": "VCC"},
        {"pin": "LOST", "net": None},
    ]
    result = eyes.pinout({"x": 0, "y": 0}, _footprint(), pins, _LAYERS)
    assert result["rows"][0]["mapping_state"] == "ambiguous"
    assert result["unmatched"] == [
        {
            "pin": "VCC",
            "pad": "99",
            "net": "VCC",
            "reason": "explicit pad has no geometry",
        },
        {"pin": "LOST", "pad": None, "net": None, "reason": "no physical pad mapping"},
    ]


def test_identity_is_not_independent_signal_and_duplicate_can_be_connected():
    fp = {
        "pads": [{"number": "1", "x": 0, "y": 0}, {"number": "1", "x": 2, "y": 0}],
        "pin_map": {"1": {"name": "1"}},
    }
    rows = eyes.pinout(
        {"x": 0, "y": 0}, fp, [{"pin": "SIGNAL", "pad": "1", "net": "N"}], _LAYERS
    )["rows"]
    assert all(r["mapping_state"] == "connected" for r in rows)
    assert rows[0]["mapping_sources"] == [
        {"pin": "SIGNAL", "nets": ["N"], "sources": ["explicit-pin-pad"]}
    ]
    assert rows[0]["duplicate_indices"] == [1, 2]


def test_malformed_geometry_never_becomes_a_pad_at_zero():
    fp = {
        "pads": [
            None,
            {"number": "", "x": 1, "y": 2},
            {"number": "X", "x": float("nan"), "y": 1},
            {"number": "Y", "x": 1e308, "y": 0},
        ]
    }
    rows = eyes.pinout({"x": 1e308, "y": 0}, fp, [], _LAYERS)["rows"]
    assert len(rows) == 4
    assert all(r["board_x_mm"] is None for r in rows)
    assert all(r["geometry"] == "invalid_geometry" for r in rows[:3])
    assert (
        eyes.pinout({"x": 0, "y": 0, "rot": float("nan")}, _footprint(), [], _LAYERS)[
            "placed"
        ]
        is False
    )


@pytest.fixture
def board(store):
    pcb = PcbHandler(hub=Hub(store=store))
    fp = _footprint()
    # Existing authoring schema uses pin for local pads; stored shape uses number.
    pads = [{**p, "pin": p["number"]} for p in fp["pads"]]
    pcb.put(
        id="pinout-test",
        args={
            "footprints": [{"name": "ASYM", "pads": pads, "pin_map": fp["pin_map"]}],
            "components": [
                {
                    "refdes": refdes,
                    "footprint": "ASYM",
                    "x": x,
                    "y": y,
                    "rot": rot,
                    "layer": side,
                    "pins": [
                        {"name": name, "pad": str(i)}
                        for i, name in enumerate(_NAMES, 1)
                    ],
                }
                for refdes, x, y, rot, side in [
                    ("J_TOP", 10, 20, 0, "top"),
                    ("J_BOTTOM", 30, 40, 90, "bottom"),
                ]
            ]
            + [
                {"refdes": "J_UNPLACED", "footprint": "ASYM"},
                {"refdes": "J_MISSING", "footprint": "NOT_CACHED"},
            ],
            "connections": [
                {"refdes": refdes, "pin": name, "net": f"{refdes}_{name}"}
                for refdes in ("J_TOP", "J_BOTTOM")
                for name in _NAMES[:4]
            ],
        },
    )
    return pcb


def _snapshot(store):
    tables = [
        "refs",
        "chunks",
        "pcb_boards",
        "pcb_components",
        "pcb_instances",
        "pcb_pins",
        "pcb_nets",
        "pcb_netconns",
        "pcb_local_footprints",
        "part_footprints",
    ]
    with store.pool.connection() as conn:
        return {
            table: sorted(
                json.dumps(r[0], sort_keys=True, default=str)
                for r in conn.execute(f"SELECT to_jsonb(t) FROM {table} t").fetchall()
            )
            for table in tables
        }


def test_native_handler_rows_and_database_read_only(board, store, monkeypatch):
    before = _snapshot(store)

    def forbidden(*args, **kwargs):
        pytest.fail("pinout attempted a provider fetch or enqueue")

    monkeypatch.setattr("precis.handlers.pcb.fetch_footprint", forbidden)
    monkeypatch.setattr(board, "_queue_datasheets", forbidden)
    with store.pool.connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        with monkeypatch.context() as patch:
            patch.setattr(
                store.pool, "connection", lambda *args, **kwargs: nullcontext(conn)
            )
            front = board.get(id="pinout-test#J_TOP", view="pinout").body
            bottom = board.get(id="pinout-test#J_BOTTOM", view="pinout").body
            unplaced = board.get(id="pinout-test#J_UNPLACED", view="pinout").body
            missing = board.get(id="pinout-test#J_MISSING", view="pinout").body
    assert "design-local-authored" in front and "physical pads: 7" in front
    assert "8.0000" in front and "21.0000" in front
    assert "31.0000" in bottom and "38.0000" in bottom and "B.Cu" in bottom
    assert "explicit-pin-pad" in bottom and "J_BOTTOM_VCC" in bottom
    assert "unconnected" in front and "unclaimed" in front and "[6, 7]" in front
    assert "board coordinates: unavailable (unplaced/invalid pose)" in unplaced
    assert "geometry: unavailable" in missing and "precis-pcb-help" in missing
    assert before == _snapshot(store)


@pytest.mark.parametrize(
    "selector",
    [
        None,
        "",
        "/",
        "pinout-test",
        "#J_TOP",
        "pinout-test#",
        "pinout-test@VCC",
        "pinout-test#J#2",
        "pinout-test#J@N",
    ],
)
def test_pinout_requires_one_instance_without_dumping_boards(board, selector):
    with pytest.raises(BadInput) as exc:
        board.get(id=selector, view="pinout")
    assert "view='pinout'" in str(exc.value.next)


def test_unknown_instance_and_board_are_typed_and_legacy_selectors_stay(board):
    assert "pinout" in board.spec.views
    with pytest.raises(NotFound) as exc:
        board.get(id="pinout-test#MISSING", view="pinout")
    assert "id='pinout-test'" in str(exc.value.next)
    with pytest.raises(NotFound):
        board.get(id="no-board#J1", view="pinout")
    logical = board.get(id="pinout-test#J_TOP").body
    assert board.get(id="pinout-test#J_TOP", view="footprints").body == logical
    assert "neighbors" in logical and "local_x_mm" not in logical
    assert "J_TOP_VCC" in board.get(id="pinout-test@J_TOP_VCC").body
    assert "pinout-test" in board.get(id="pinout-test").body
    assert (
        "no catalog-part instances"
        in board.get(id="pinout-test", view="footprints").body
    )
    assert "pinout-test" in board.get().body


@pytest.mark.parametrize("source", ["easyeda:packageDetail:test", "authored", None])
def test_catalog_provenance_and_missing_cache_does_not_use_local(board, store, source):
    store.pcb_apply(
        slug="pinout-test",
        title="pinout test",
        nets=[],
        connections=[],
        components=[
            {
                "refdes": "J_CATALOG",
                "part": "C999999999",
                "footprint": "ASYM",
                "x": 60,
                "y": 60,
            }
        ],
    )
    missing = board.get(id="pinout-test#J_CATALOG", view="pinout").body
    assert "geometry: unavailable" in missing and "'op':'footprint'" in missing
    store.part_footprint_put("C999999999", {**_footprint(), "source": source})
    read = board.get(id="pinout-test#J_CATALOG", view="pinout").body
    assert f"catalog-cache; source={source or 'unknown'}" in read
    assert "physical pads: 7" in read and "design-local-authored" not in read


def test_handler_reports_unmatched_declared_pins(board, store):
    store.pcb_apply(
        slug="pinout-test",
        title="pinout test",
        nets=[],
        connections=[],
        components=[
            {
                "refdes": "J_UNMATCHED",
                "footprint": "ASYM",
                "x": 90,
                "y": 90,
                "pins": [{"name": "LOST", "pad": "99"}],
            }
        ],
    )
    text = board.get(id="pinout-test#J_UNMATCHED", view="pinout").body
    assert "unmatched declared pins:" in text
    assert "LOST" in text and "explicit pad has no geometry" in text
