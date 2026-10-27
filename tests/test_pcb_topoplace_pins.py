"""Slice 1b: slot feasibility (Decision 12), Hungarian + 2-opt pin
assignment (Decision 11), swap-group moves (Decision 13) — per spec
§1b's own test list. Wheel-side tests (``Part.pin``/``pin_functions``/
``tie``) live in ``tests/workspaces/test_pcb_pins.py``."""

from __future__ import annotations

import math

import numpy as np
import pytest

from precis.pcb.geom import segments_cross
from precis.pcb.topoplace import (
    FlexSlot,
    PinAssignment,
    SwapGroup,
    TopoDesign,
    assign_pins,
    check_slots,
)
from precis.pcb.topoplace.writeback import pins_h
from precis_geom.pcb import PinConflict


def _design(
    *,
    parts: tuple[str, ...],
    pin_xy: list[list[float]],
    pin_part: list[int],
    pin_label: list[str],
    pin_net: list[int],
    net_pins: list[list[int]] | None = None,
    flex: tuple[FlexSlot, ...] = (),
    swap_groups: tuple[SwapGroup, ...] = (),
    claims: tuple[tuple[int, str], ...] = (),
    decl_nets: tuple[list[int], ...] = (),
    decl_net_ir: list[int] | None = None,
    decl_net_route: list[int] | None = None,
    decl_net_plane: list[bool] | None = None,
    decl_net_name: tuple[str, ...] = (),
    anchor_xy: list[list[float]] | None = None,
    anchor_net: tuple[str, ...] = (),
    net_anchors: list[list[int]] | None = None,
    pose0: list[list[float]],
) -> TopoDesign:
    n_d = len(decl_nets)
    return TopoDesign(
        parts=parts,
        pin_xy=np.asarray(pin_xy, dtype=np.float64),
        pin_part=np.asarray(pin_part, dtype=np.int64),
        pin_side=np.zeros(len(pin_xy), dtype=np.int8),
        pin_label=np.asarray(pin_label, dtype=object),
        pin_net=np.asarray(pin_net, dtype=np.int32),
        net_pins=tuple(np.asarray(n, dtype=np.int64) for n in (net_pins or [])),
        flex=flex,
        swap_groups=swap_groups,
        fixed=np.zeros(len(parts), dtype=bool),
        freedoms=np.full(len(parts), 3, dtype=np.uint8),
        outline=np.asarray([[-5, -5], [30, -5], [30, 30], [-5, 30]]),
        pose0=np.asarray(pose0, dtype=np.float64),
        claims=claims,
        decl_nets=tuple(np.asarray(n, dtype=np.int64) for n in decl_nets),
        decl_net_ir=np.asarray(decl_net_ir or [-1] * n_d, dtype=np.int32),
        decl_net_route=np.asarray(decl_net_route or [-1] * n_d, dtype=np.int64),
        decl_net_plane=np.asarray(decl_net_plane or [False] * n_d, dtype=bool),
        decl_net_name=decl_net_name,
        anchor_xy=np.asarray(anchor_xy or [], dtype=np.float64).reshape(-1, 2),
        anchor_net=anchor_net,
        net_anchors=tuple(np.asarray(a, dtype=np.int64) for a in (net_anchors or [])),
    )


def _slot(part: int, pins: list[int], net: int, name: str | None = None) -> FlexSlot:
    return FlexSlot(part=part, pins=tuple(pins), net=net, name=name)


def _pt(xy: np.ndarray) -> tuple[float, float]:
    return (float(xy[0]), float(xy[1]))


def _added_wires(
    d: TopoDesign, a: PinAssignment, pw: np.ndarray
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """The wire each slot picked added: chosen pin -> the one wired
    member of its net (every test net here is single-member)."""
    out = []
    for i, s in enumerate(d.flex):
        dest = [p for p in d.decl_nets[s.net] if p not in set(s.pins)]
        assert len(dest) == 1
        out.append((_pt(pw[a.choice[i]]), _pt(pw[dest[0]])))
    return out


def _crosses(wires: list[tuple[tuple[float, float], tuple[float, float]]]) -> int:
    return sum(
        segments_cross(w1[0], w1[1], w2[0], w2[1])
        for i, w1 in enumerate(wires)
        for w2 in wires[i + 1 :]
    )


def _world(d: TopoDesign) -> np.ndarray:
    from precis.pcb.topoplace.proxy import all_pin_world

    return all_pin_world(d, d.pose0)


def test_fixed_pin_leaves_any_of() -> None:
    """A pin already wired to a DIFFERENT net leaves the slot's any_of;
    left with nothing it raises PinConflict (Decision 12)."""
    # U1 pins 1,2; pin 1 wired to net 0; slot on net 1 wants only pin 1.
    d = _design(
        parts=("U1", "J1"),
        pin_xy=[[0, 0], [1, 0], [10, 0]],
        pin_part=[0, 0, 1],
        pin_label=["1", "2", "1"],
        pin_net=[0, -1, 1],
        flex=(_slot(0, [0], 0, "SIG"),),
        decl_nets=([2],),
        decl_net_ir=[1],
        decl_net_name=("SIG",),
        pose0=[[0, 0, 0, 0], [10, 0, 0, 0]],
    )
    # pin 0 is wired to ir net 0; the slot's net is ir net 1 -> no
    # feasible candidate
    with pytest.raises(PinConflict, match="no feasible pin"):
        check_slots(d)

    # same pin wired to the SLOT's own net stays a candidate
    d2 = _design(
        parts=("U1", "J1"),
        pin_xy=[[0, 0], [1, 0], [10, 0]],
        pin_part=[0, 0, 1],
        pin_label=["1", "2", "1"],
        pin_net=[1, -1, 1],
        flex=(_slot(0, [0, 1], 0, "SIG"),),
        decl_nets=([0, 2],),
        decl_net_ir=[1],
        decl_net_name=("SIG",),
        pose0=[[0, 0, 0, 0], [10, 0, 0, 0]],
    )
    check_slots(d2)
    a = assign_pins(d2, d2.pose0)
    assert a.by_name["SIG"] == ("U1", 1)


def test_claimed_pin_leaves_any_of() -> None:
    """A named fixed claim is a fixed pin too (Decision 12): it leaves
    every slot's any_of."""
    d = _design(
        parts=("U1", "J1"),
        pin_xy=[[0, 0], [1, 0], [10, 0]],
        pin_part=[0, 0, 1],
        pin_label=["1", "2", "1"],
        pin_net=[-1, -1, 0],
        flex=(_slot(0, [0, 1], 0, "SIG"),),
        claims=((0, "BTN"),),
        decl_nets=([2],),
        decl_net_ir=[0],
        decl_net_name=("SIG",),
        pose0=[[0, 0, 0, 0], [10, 0, 0, 0]],
    )
    a = assign_pins(d, d.pose0)
    assert a.by_name["SIG"] == ("U1", 2)  # pin 1 claimed, pin 2 chosen
    assert a.by_name["BTN"] == ("U1", 1)


def test_hall_message() -> None:
    """Three slots, two pins: the König witness names the signals and
    the pins (spec's exact message shape)."""
    flex = (
        _slot(0, [0, 1], 0, "BTN"),
        _slot(0, [0, 1], 1, "INT2"),
        _slot(0, [0, 1], 2, "INT3"),
    )
    d = _design(
        parts=("U1", "J1"),
        pin_xy=[[0, 0], [1, 0], [10, 0], [10, 1], [10, 2]],
        pin_part=[0, 0, 1, 1, 1],
        pin_label=["1", "4", "1", "2", "3"],
        pin_net=[-1, -1, 0, 1, 2],
        flex=flex,
        decl_nets=([2], [3], [4]),
        decl_net_ir=[0, 1, 2],
        decl_net_name=("A", "B", "C"),
        pose0=[[0, 0, 0, 0], [10, 0, 0, 0]],
    )
    with pytest.raises(PinConflict) as exc:
        check_slots(d)
    assert str(exc.value) == "BTN, INT2, INT3 need [1,4]: 3 signals, 2 pins"


def test_connector_row_zero_crossings() -> None:
    """8 slots whose destinations sit on one connector row: the sorted
    fast path assigns with 0 crossings among them (spec test)."""
    rng = np.random.default_rng(3)
    order = rng.permutation(8)
    # U1 candidate pins on a row; J2 destination pins on a row.
    pin_xy = [[float(i), 0.0] for i in range(8)] + [[0.0, float(i)] for i in range(8)]
    pin_part = [0] * 8 + [1] * 8
    pin_label = [str(i + 1) for i in range(8)] * 2
    pin_net = [-1] * 8 + [int(order[i]) for i in range(8)]
    flex = tuple(_slot(0, list(range(8)), int(order[i]), f"S{i}") for i in range(8))
    d = _design(
        parts=("U1", "J2"),
        pin_xy=pin_xy,
        pin_part=pin_part,
        pin_label=pin_label,
        pin_net=pin_net,
        flex=flex,
        decl_nets=tuple([8 + i] for i in range(8)),
        decl_net_ir=list(range(8)),
        decl_net_name=tuple(f"N{i}" for i in range(8)),
        pose0=[[0, 0, 0, 0], [20, 0, 0, 0]],
    )
    a = assign_pins(d, d.pose0)
    assert _crosses(_added_wires(d, a, _world(d))) == 0


def test_hungarian_no_self_crossings() -> None:
    """Seeded random: the min-length Euclidean assignment has no
    crossings among its own wires (Decision 11)."""
    rng = np.random.default_rng(7)
    n = 10
    pin_xy = (
        np.column_stack([rng.uniform(0, 5, n), rng.uniform(0, 5, n)]).tolist()
        + np.column_stack([rng.uniform(10, 15, n), rng.uniform(0, 10, n)]).tolist()
    )
    pin_part = [0] * n + [1] * n
    pin_label = [str(i) for i in range(2 * n)]
    pin_net = [-1] * n + list(range(n))
    flex = tuple(_slot(0, list(range(n)), i, f"S{i}") for i in range(n))
    d = _design(
        parts=("U1", "J2"),
        pin_xy=pin_xy,
        pin_part=pin_part,
        pin_label=pin_label,
        pin_net=pin_net,
        flex=flex,
        decl_nets=tuple([n + i] for i in range(n)),
        decl_net_ir=list(range(n)),
        decl_net_name=tuple(f"N{i}" for i in range(n)),
        pose0=[[0, 0, 0, 0], [0, 0, 0, 0]],
    )
    a = assign_pins(d, d.pose0)
    assert _crosses(_added_wires(d, a, _world(d))) == 0
    assert len(set(a.choice)) == n  # a real matching: distinct pins


def test_2opt_never_raises_and_helps() -> None:
    """Declaration order crossed; the solver's answer is at or under
    it (spec: 2-opt never raises the crossing count)."""
    # Two slots, two pins; destinations force the greedy order to cross.
    d = _design(
        parts=("U1", "J1"),
        pin_xy=[[0, 0], [0, 1], [10, 0], [10, 1]],
        pin_part=[0, 0, 1, 1],
        pin_label=["1", "2", "1", "2"],
        pin_net=[-1, -1, 0, 1],
        flex=(_slot(0, [0, 1], 0, "TOP"), _slot(0, [0, 1], 1, "BOT")),
        decl_nets=([3], [2]),  # TOP's dest is the BOTTOM pin and vv
        decl_net_ir=[0, 1],
        decl_net_name=("A", "B"),
        pose0=[[0, 0, 0, 0], [10, 0, 0, 0]],
    )
    a = assign_pins(d, d.pose0)
    assert _crosses(_added_wires(d, a, _world(d))) == 0
    assert a.cost_vs_fixed < 0


def test_anchor_survives_an_accepted_move() -> None:
    """gr520903 review: ``_delta``/``_apply`` rebuilt affected nets'
    wires without the ``net_pins`` slot arg, so a fixed-via anchor was
    dropped the first time a move touched its net — the wire model
    silently lost the anchor it was built with. After the member
    exchange is accepted, net 0's wires still end at the anchor."""
    from precis.pcb.topoplace import pins as _pins
    from precis.pcb.topoplace.proxy import all_pin_world

    d = _design(
        parts=("RN1", "J1"),
        pin_xy=[[0, 1], [0, -1], [10, 1], [10, -1]],
        pin_part=[0, 0, 1, 1],
        pin_label=["1", "2", "1", "2"],
        pin_net=[0, 1, 0, 1],
        net_pins=[[0, 3], [1, 2]],
        swap_groups=(SwapGroup(part=0, members=((0,), (1,))),),
        decl_nets=([0, 3], [1, 2]),
        decl_net_ir=[0, 1],
        decl_net_route=[0, 1],
        decl_net_name=("A", "B"),
        anchor_xy=[[5.0, -1.0]],
        anchor_net=("A",),
        net_anchors=[[0], []],
        pose0=[[0, 0, 0, 0], [10, 0, 0, 0]],
    )
    pw = all_pin_world(d, d.pose0)
    effs = _pins._effective(d)  # no flex slots — the swap group moves
    m = _pins._model(d, pw, effs, [], None)
    swaps: list[tuple[str, int | str, int | str]] = []
    _pins._two_opt(d, m, effs, [], swaps)
    assert swaps == [("RN1", 1, 2)]
    assert any(
        np.allclose(w[:2], [5.0, -1.0]) or np.allclose(w[2:], [5.0, -1.0])
        for w in m.wires[0]
    )


def test_swap_group_plain_member() -> None:
    """A plain group permutes its members' nets: the crossed wiring
    uncrosses and the move is recorded (Decision 13)."""
    # RN1 pin 1 (top) wired to the bottom destination, pin 2 (bottom)
    # to the top one — one member exchange uncrosses.
    d = _design(
        parts=("RN1", "J1"),
        pin_xy=[[0, 1], [0, -1], [10, 1], [10, -1]],
        pin_part=[0, 0, 1, 1],
        pin_label=["1", "2", "1", "2"],
        pin_net=[0, 1, 0, 1],
        net_pins=[[0, 3], [1, 2]],
        swap_groups=(SwapGroup(part=0, members=((0,), (1,))),),
        decl_nets=([0, 3], [1, 2]),
        decl_net_ir=[0, 1],
        decl_net_route=[0, 1],
        decl_net_name=("A", "B"),
        pose0=[[0, 0, 0, 0], [10, 0, 0, 0]],
    )
    a = assign_pins(d, d.pose0)
    assert a.swaps == (("RN1", 1, 2),)
    assert a.cost_vs_fixed < 0


def test_tuple_group_moves_together() -> None:
    """``in_i``/``out_i`` move as units: the tuple exchange uncrosses
    the channels pairwise (spec test)."""
    # Two channels; unit 1's pins sit top, wired to the bottom
    # destinations; unit 2 bottom -> top. Exchanging the UNITS
    # (position-wise) gives 0 crossings.
    d = _design(
        parts=("RN1", "J1"),
        pin_xy=[
            [-1, 1],
            [1, 1],
            [-1, -1],
            [1, -1],
            [10, -2],
            [20, -2],
            [10, 2],
            [20, 2],
        ],
        pin_part=[0, 0, 0, 0, 1, 1, 1, 1],
        pin_label=["in1", "out1", "in2", "out2", "a", "b", "c", "d"],
        pin_net=[0, 1, 2, 3, 0, 1, 2, 3],
        net_pins=[[0, 4], [1, 5], [2, 6], [3, 7]],
        swap_groups=(SwapGroup(part=0, members=((0, 1), (2, 3)), tuple_=True),),
        decl_nets=([0, 4], [1, 5], [2, 6], [3, 7]),
        decl_net_ir=[0, 1, 2, 3],
        decl_net_route=[0, 1, 2, 3],
        decl_net_name=("A", "B", "C", "D"),
        pose0=[[0, 0, 0, 0], [10, 0, 0, 0]],
    )
    a = assign_pins(d, d.pose0)
    # one unit exchange = one recorded pair per position
    assert a.swaps == (("RN1", "in1", "in2"), ("RN1", "out1", "out2"))
    assert a.cost_vs_fixed < 0


def test_requirement_filters_rated_pins() -> None:
    """Decision 15 at solve time: a candidate RATED under the slot's
    ``current=Max`` bound leaves the set; unrated stays."""
    slot = FlexSlot(
        part=0,
        pins=(0, 1),
        net=0,
        name="PWR",
        requirement_a=0.05,
        pin_max_a=(0.01, math.nan),
    )
    d = _design(
        parts=("U1", "J1"),
        pin_xy=[[0, 0], [1, 0], [10, 0]],
        pin_part=[0, 0, 1],
        pin_label=["1", "2", "1"],
        pin_net=[-1, -1, 0],
        flex=(slot,),
        decl_nets=([2],),
        decl_net_ir=[0],
        decl_net_name=("P",),
        pose0=[[0, 0, 0, 0], [10, 0, 0, 0]],
    )
    a = assign_pins(d, d.pose0)
    assert a.by_name["PWR"] == ("U1", 2)  # pin 1 under-rated


def test_pins_h_one_define_per_name_and_determinism() -> None:
    """``pins.h`` carries one ``#define <NAME>_PIN`` per name and a
    rerun gives the same bytes (Decisions 14, 22)."""
    d = _design(
        parts=("U1", "J1"),
        pin_xy=[[0, 0], [1, 0], [10, 0]],
        pin_part=[0, 0, 1],
        pin_label=["1", "4", "1"],
        pin_net=[-1, -1, 0],
        flex=(_slot(0, [0, 1], 0, "BTN"),),
        claims=((1, "FIX"),),
        decl_nets=([2],),
        decl_net_ir=[0],
        decl_net_name=("S",),
        pose0=[[0, 0, 0, 0], [10, 0, 0, 0]],
    )
    a = assign_pins(d, d.pose0)
    h1 = pins_h(d, a)
    h2 = pins_h(d, assign_pins(d, d.pose0))
    assert h1 == h2
    assert "#define BTN_PIN" in h1
    assert "#define FIX_PIN" in h1
    # claim pins the same pin BTN cannot take
    assert "/* U1 */" in h1
