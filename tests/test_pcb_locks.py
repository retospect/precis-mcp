"""pcb-stackup-regions Slice 2 — lock and region columns: put round-trips,
refusals, and the views that print them. Storage and views only; the placer
does not read the new columns (tests/test_pcb_optimize.py pins that)."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import padplace


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


_BOARD: dict[str, Any] = {
    "footprints": [
        {
            "name": "asym",
            "pads": [{"pin": "1", "shape": "rect", "x": 1.0, "y": 0.0, "w": 1, "h": 1}],
        },
        {
            "name": "tht",
            "pads": [
                {
                    "pin": "1",
                    "shape": "circle",
                    "x": 0.0,
                    "y": 0.0,
                    "w": 1.6,
                    "h": 1.6,
                    "drill": 0.8,
                }
            ],
        },
    ],
    "components": [
        {
            "refdes": "U1",
            "label": "ic",
            "footprint": "asym",
            "x": 10.0,
            "y": 10.0,
            "pins": [{"name": "1"}],
        },
        {
            "refdes": "J1",
            "label": "hdr",
            "footprint": "tht",
            "x": 30.0,
            "y": 10.0,
            "pins": [{"name": "1"}],
        },
        {
            "refdes": "R1",
            "label": "r",
            "x": 50.0,
            "y": 10.0,
            "max_travel_mm": 2.5,
            "pins": [{"name": "1"}],
        },
    ],
    "nets": [{"name": "N1", "class": "signal"}],
    "connections": [
        {"net": "N1", "refdes": "U1", "pin": "1"},
        {"net": "N1", "refdes": "J1", "pin": "1"},
    ],
}


def _inst(store, slug: str, refdes: str) -> dict[str, Any]:
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return next(
        i for i in store.pcb_graph(ref.id)["instances"] if i["refdes"] == refdes
    )


def _col(store, slug: str, refdes: str, col: str) -> Any:
    ref = store.get_ref(kind="pcb", id=slug)
    with store.pool.connection() as conn:
        return conn.execute(
            f"SELECT {col} FROM pcb_instances WHERE ref_id = %s AND refdes = %s",
            (ref.id, refdes),
        ).fetchone()[0]


def _move(pcb, slug: str, refdes: str = "U1", **kw: Any):
    return pcb.put(id=slug, args={"op": "move", "refdes": refdes, **kw})


def _board(pcb, slug: str) -> None:
    pcb.put(id=slug, args=_BOARD)


def test_authored_leash_lands_in_the_column_not_meta(pcb, store):
    _board(pcb, "lk-insert")
    assert _col(store, "lk-insert", "R1", "max_travel_mm") == 2.5
    assert "max_travel_mm" not in _col(store, "lk-insert", "R1", "meta")
    assert _inst(store, "lk-insert", "R1")["max_travel_mm"] == 2.5


def test_lock_free_graph_has_no_lock_keys(pcb, store):
    _board(pcb, "lk-free")
    inst = _inst(store, "lk-free", "U1")
    assert not {"max_travel_mm", "fixed_side", "group_fixed", "room_id"} & inst.keys()


def test_move_round_trips_every_lock_column(pcb, store):
    _board(pcb, "lk-rt")
    pcb.put(
        id="lk-rt",
        args={"features": [{"ftype": "room", "x": 0.0, "y": 0.0, "geom": {}}]},
    )
    ref = store.get_ref(kind="pcb", id="lk-rt")
    room = next(f for f in store.pcb_features_list(ref.id) if f["ftype"] == "room")
    _move(
        pcb,
        "lk-rt",
        fixed_side=True,
        group_fixed=True,
        room_id=room["feature_id"],
        max_travel_mm=1.5,
        side="bottom",
    )
    inst = _inst(store, "lk-rt", "U1")
    assert inst["layer"] == "bottom"
    assert inst["fixed_side"] is True
    assert inst["group_fixed"] is True
    assert inst["room_id"] == room["feature_id"]
    assert inst["max_travel_mm"] == 1.5
    _move(
        pcb,
        "lk-rt",
        fixed_side=False,
        group_fixed=False,
        room_id=None,
        max_travel_mm=None,
    )
    inst = _inst(store, "lk-rt", "U1")
    assert not {"max_travel_mm", "fixed_side", "group_fixed", "room_id"} & inst.keys()
    assert inst["layer"] == "bottom"


def test_pose_move_still_applies_the_lock_columns(pcb, store):
    _board(pcb, "lk-pose")
    _move(pcb, "lk-pose", x=12.0, y=10.0, max_travel_mm=0.75, group_fixed=True)
    inst = _inst(store, "lk-pose", "U1")
    assert (inst["x"], inst["max_travel_mm"], inst["group_fixed"]) == (12.0, 0.75, True)


def test_side_bottom_flips_layer_and_mirrors_pads(pcb, store):
    _board(pcb, "lk-side")
    fp = {"x": 1.0, "y": 0.0, "w": 1.0, "h": 1.0, "shape": "rect", "pin": "1"}

    def pad_x() -> float:
        placed, _ = padplace.place_footprint_pads(
            [fp], _inst(store, "lk-side", "U1"), layers=["F.Cu", "B.Cu"]
        )
        return placed[0]["x"]

    assert pad_x() == pytest.approx(11.0)
    _move(pcb, "lk-side", side="bottom")
    assert _col(store, "lk-side", "U1", "layer") == "bottom"
    assert pad_x() == pytest.approx(9.0)  # mirrored about the part origin
    _move(pcb, "lk-side", side="top")
    assert pad_x() == pytest.approx(11.0)


@pytest.mark.parametrize("bad", ["left", "BOTTOM", None, 1])
def test_bad_side_refused(pcb, bad):
    _board(pcb, "lk-bad-side")
    with pytest.raises(BadInput, match=r"pcb: side must be 'top' or 'bottom'"):
        _move(pcb, "lk-bad-side", side=bad)


def test_negative_leash_refused(pcb):
    _board(pcb, "lk-neg")
    with pytest.raises(BadInput, match="max_travel_mm"):
        _move(pcb, "lk-neg", max_travel_mm=-0.1)


@pytest.mark.parametrize("key", ["fixed_side", "group_fixed"])
@pytest.mark.parametrize("bad", ["true", 1, 0, "yes"])
def test_non_boolean_lock_refused(pcb, key, bad):
    _board(pcb, "lk-nonbool")
    with pytest.raises(BadInput, match=f"{key} must be a boolean"):
        _move(pcb, "lk-nonbool", **{key: bad})


def test_room_id_must_be_a_live_room_on_this_board(pcb, store):
    _board(pcb, "lk-room")
    _board(pcb, "lk-room-other")
    pcb.put(
        id="lk-room",
        args={
            "features": [
                {"ftype": "mounting_hole", "x": 5.0, "y": 5.0, "geom": {"diameter": 3}}
            ]
        },
    )
    pcb.put(
        id="lk-room-other",
        args={"features": [{"ftype": "room", "x": 0.0, "y": 0.0, "geom": {}}]},
    )
    ref = store.get_ref(kind="pcb", id="lk-room")
    hole = next(f for f in store.pcb_features_list(ref.id) if f["ftype"] != "outline")
    other = store.get_ref(kind="pcb", id="lk-room-other")
    foreign = next(f for f in store.pcb_features_list(other.id) if f["ftype"] == "room")
    hid = hole["feature_id"]
    with pytest.raises(
        BadInput, match=f"feature {hid} is a 'mounting_hole', not a room"
    ):
        _move(pcb, "lk-room", room_id=hid)
    fid = foreign["feature_id"]
    with pytest.raises(BadInput, match=f"no live feature {fid} on this board"):
        _move(pcb, "lk-room", room_id=fid)
    with pytest.raises(BadInput, match="room_id must be an integer"):
        _move(pcb, "lk-room", room_id="12")
    assert "room_id" not in _inst(store, "lk-room", "U1")


def test_side_refused_on_a_through_hole_part(pcb, store):
    _board(pcb, "lk-tht")
    with pytest.raises(BadInput, match="J1 is a through-hole part"):
        _move(pcb, "lk-tht", "J1", side="bottom")
    assert _col(store, "lk-tht", "J1", "layer") == "top"


def test_side_flip_refused_while_side_locked_unless_unlocked_in_the_call(pcb, store):
    _board(pcb, "lk-locked")
    _move(pcb, "lk-locked", fixed_side=True)
    with pytest.raises(
        BadInput,
        match=r"pcb: U1 is side-locked \(fixed_side=true\); pass fixed_side=false "
        r"to unlock it",
    ):
        _move(pcb, "lk-locked", side="bottom")
    assert _col(store, "lk-locked", "U1", "layer") == "top"
    # same side is a no-op, not a refusal
    _move(pcb, "lk-locked", side="top")
    # unlock and flip in one call
    _move(pcb, "lk-locked", side="bottom", fixed_side=False)
    inst = _inst(store, "lk-locked", "U1")
    assert inst["layer"] == "bottom" and "fixed_side" not in inst


def test_move_needs_something(pcb):
    _board(pcb, "lk-empty")
    with pytest.raises(BadInput, match="side / fixed_side / group_fixed / room_id"):
        _move(pcb, "lk-empty")


def test_toc_and_placement_view_print_side_and_lock(pcb, store):
    _board(pcb, "lk-view")
    free = pcb.get(id="lk-view", view="placement").body
    assert "free" in free and "leash 2.5 mm" in free and "locked" not in free
    _move(pcb, "lk-view", fixed="xy", fixed_side=True, group_fixed=True, side="bottom")
    body = pcb.get(id="lk-view", view="placement").body
    row = next(line for line in body.splitlines() if "U1" in line)
    assert "bottom" in row and "locked" in row
    assert "side-locked" in row and "group-fixed" in row
    toc = pcb.get(id="lk-view").body
    assert "side-locked" in toc and "leash 2.5 mm" in toc


def test_toc_of_a_lock_free_board_has_no_lock_columns(pcb):
    # R1 carries a leash in _BOARD; build the board without it
    plain = {
        **_BOARD,
        "components": [
            {k: v for k, v in c.items() if k != "max_travel_mm"}
            for c in _BOARD["components"]
        ],
    }
    pcb.put(id="lk-plain2", args=plain)
    toc = pcb.get(id="lk-plain2").body
    assert "flags" not in toc


def test_plane_priority_and_region_round_trip(pcb, store):
    _board(pcb, "lk-plane")
    ref = store.get_ref(kind="pcb", id="lk-plane")
    board_id = int(store.pcb_graph(ref.id)["board"]["board_id"])
    pcb.put(id="lk-plane", args={"op": "plane_net", "layer": "In1.Cu", "net": "N1"})
    row = store.pcb_planes_list(ref.id)[0]
    assert (row["region"], row["priority"]) == (None, 0)
    assert "whole board" in pcb.get(id="lk-plane", view="planes").body

    pcb.put(
        id="lk-plane",
        args={"op": "plane_net", "layer": "In1.Cu", "net": "N1", "priority": 3},
    )
    assert store.pcb_planes_list(ref.id)[0]["priority"] == 3
    # an omitted priority leaves the stored one alone
    pcb.put(id="lk-plane", args={"op": "plane_net", "layer": "In1.Cu", "net": "N1"})
    assert store.pcb_planes_list(ref.id)[0]["priority"] == 3
    with pytest.raises(BadInput, match="priority must be an integer"):
        pcb.put(
            id="lk-plane",
            args={"op": "plane_net", "layer": "In1.Cu", "net": "N1", "priority": "hi"},
        )
    assert "3" in pcb.get(id="lk-plane", view="planes").body

    # derived write-back carries region / priority when the spec is a dict
    poly = [[0, 0], [5, 0], [5, 5]]
    store.pcb_planes_replace_derived(
        ref.id,
        board_id,
        {"N1": {"layer": "B.Cu", "region": poly, "priority": 2}},
    )
    derived = next(r for r in store.pcb_planes_list(ref.id) if r["layer"] == "B.Cu")
    assert (derived["region"], derived["priority"]) == (poly, 2)
    assert "polygon 3 pts" in pcb.get(id="lk-plane", view="planes").body
    # the plain str form still means whole board / priority 0
    store.pcb_planes_replace_derived(ref.id, board_id, {"N1": "B.Cu"})
    derived = next(r for r in store.pcb_planes_list(ref.id) if r["layer"] == "B.Cu")
    assert (derived["region"], derived["priority"]) == (None, 0)


def test_retired_room_is_not_reported(pcb, store):
    _board(pcb, "lk-retired")
    pcb.put(
        id="lk-retired",
        args={"features": [{"ftype": "room", "x": 0.0, "y": 0.0, "geom": {}}]},
    )
    ref = store.get_ref(kind="pcb", id="lk-retired")
    room = next(f for f in store.pcb_features_list(ref.id) if f["ftype"] == "room")
    _move(pcb, "lk-retired", room_id=room["feature_id"])
    assert _inst(store, "lk-retired", "U1")["room_id"] == room["feature_id"]
    assert "room " in pcb.get(id="lk-retired", view="placement").body
    store.pcb_features_retire(ref.id, [room["feature_id"]])
    assert "room_id" not in _inst(store, "lk-retired", "U1")
    assert "room " not in pcb.get(id="lk-retired", view="placement").body


def test_side_flip_onto_an_occupied_spot_is_refused(pcb, store):
    design = {
        "components": [
            {"refdes": "A", "label": "ic", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
            {
                "refdes": "B",
                "label": "ic",
                "x": 0.0,
                "y": 0.0,
                "layer": "bottom",
                "pins": [{"name": "1"}],
            },
        ],
    }
    pcb.put(id="lk-occupied", args=design)
    with pytest.raises(BadInput, match="courtyard_overlap with B"):
        _move(pcb, "lk-occupied", "A", side="bottom")
    assert _col(store, "lk-occupied", "A", "layer") == "top"
    # with a pose onto a free spot the flip is legal
    _move(pcb, "lk-occupied", "A", side="bottom", x=40.0, y=40.0)
    assert _col(store, "lk-occupied", "A", "layer") == "bottom"
    # and moving it back onto the occupied spot is refused on the new layer
    with pytest.raises(BadInput, match="courtyard_overlap with B"):
        _move(pcb, "lk-occupied", "A", x=0.0, y=0.0)


def test_side_refused_on_a_grouped_part(pcb, store):
    design = {
        "components": [
            {
                "refdes": "J1",
                "label": "h",
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "1"}],
                "group": "pair",
                "group_offset": {"x": 0.0, "y": 0.0, "rot": 0.0},
            },
        ],
    }
    pcb.put(id="lk-group", args=design)
    with pytest.raises(BadInput, match="group side flip is not supported yet"):
        _move(pcb, "lk-group", "J1", side="bottom")
    assert _col(store, "lk-group", "J1", "layer") == "top"


@pytest.mark.parametrize("bad", [-1, "abc", True, float("inf")])
def test_component_put_validates_max_travel(pcb, bad):
    comp = {"refdes": "A", "label": "ic", "pins": [{"name": "1"}], "max_travel_mm": bad}
    with pytest.raises(BadInput, match="max_travel_mm"):
        pcb.put(id="lk-put-leash", args={"components": [comp]})


def test_component_put_accepts_null_leash(pcb, store):
    comp = {
        "refdes": "A",
        "label": "ic",
        "pins": [{"name": "1"}],
        "max_travel_mm": None,
    }
    pcb.put(id="lk-put-null", args={"components": [comp]})
    assert _col(store, "lk-put-null", "A", "max_travel_mm") is None
