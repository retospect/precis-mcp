"""td637902 — a flipped part's pad escapes on the face the pose puts it.

usbc_attiny's GND reported "pins walled off": the placer flipped R1 to
the bottom, the mesh's pads sat R1.2 on B.Cu (``world_pads`` reads the
pose), but the layered search seeded the pin on its authored F.Cu face
(``_pin_layers`` read ``d.pin_side``). J1.5's top pad lies over R1.2,
so on F.Cu the pin's fan was walled by foreign copper on every side and
the comp stranded although the any-layer flood (``_flood_region``) met
the main tree. The route now re-reads every SMD side off the routed
pose (``realise._sides_at_pose``) before the search. These boards pin
that mechanism: a pin on a flipped part under another net's top pad
reaches the rest of its net on the bottom, and one whose bottom face is
walled too stays stranded.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from precis.pcb.topoplace.design import LAYER_FORCE_FREE, TopoDesign
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.realise import _sides_at_pose
from precis.pcb.topoplace.wires import (
    _cand_mask,
    _flood_region,
    _flood_states,
    _make_ctx,
    _net_masks,
    _pin_layers,
)

_N_LAYERS = 2
_RULE = (0.15, 0.15)  # trace, clearance
_VIA_MM = 0.6


def _board(pins: list[dict[str, Any]], n_nets: int) -> TopoDesign:
    """One part per pin dict (``x``/``y`` its pose, ``w``/``h`` the pad,
    ``side`` 0 top SMD / 2 drilled, ``net`` the net index); every part
    is authored on the top."""
    n = len(pins)
    shape = np.empty(n, dtype=object)
    poly = np.empty(n, dtype=object)
    for i in range(n):
        shape[i] = "rect"
        poly[i] = None
    return TopoDesign(
        parts=tuple(f"P{i}" for i in range(n)),
        pin_xy=np.zeros((n, 2)),
        pin_part=np.arange(n),
        pin_side=np.asarray([p.get("side", 0) for p in pins], dtype=np.int8),
        pin_label=np.asarray([str(i) for i in range(n)], dtype=object),
        pin_net=np.asarray([p["net"] for p in pins], dtype=np.int32),
        net_pins=tuple(
            np.asarray([i for i, p in enumerate(pins) if p["net"] == k], dtype=np.int64)
            for k in range(n_nets)
        ),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(n, dtype=bool),
        freedoms=np.full(n, 3, dtype=np.uint8),
        outline=np.asarray([[0.0, 0.0], [40.0, 0.0], [40.0, 40.0], [0.0, 40.0]]),
        pose0=np.asarray([[p["x"], p["y"], 0.0, 0.0] for p in pins]),
        net_layer_force=np.full(n_nets, -1, dtype=np.int8),
        net_name=tuple(f"N{k}" for k in range(n_nets)),
        pin_wh=np.asarray([[p.get("w", 0.4), p.get("h", 0.4)] for p in pins]),
        pin_shape=shape,
        pin_poly=poly,
        n_layers=_N_LAYERS,
    )


def _reach(cover_side: int) -> tuple[bool, bool, bool]:
    """Net 0: pin 0 on the top at (10, 20), pin 1 at (30, 20) on a part
    the pose flips to the bottom, under a 2 x 2 mm net-1 pad (pin 2) on
    ``cover_side`` (0 the top face only, 2 drilled — every face).
    Returns whether pin 1's layered reach meets pin 0's when seeded on
    the posed face, on the authored face, and the any-layer flood."""
    d = _board(
        [
            {"x": 10.0, "y": 20.0, "net": 0},
            {"x": 30.0, "y": 20.0, "net": 0},
            {"x": 30.0, "y": 20.0, "net": 1, "w": 2.0, "h": 2.0, "side": cover_side},
        ],
        n_nets=2,
    )
    pose = d.pose0.copy()
    pose[1, 3] = 1.0  # P1 to the bottom
    mesh = build_mesh(d, pose)
    ctx = _make_ctx(mesh, d, _N_LAYERS, pose)
    masks = _net_masks(ctx, d, 0, _N_LAYERS, *_RULE, _VIA_MM)
    cand = _cand_mask(frozenset(range(_N_LAYERS)), _N_LAYERS)
    cand_l = list(range(_N_LAYERS))

    def states(pin: int, lay: int) -> set[tuple[int, int]]:
        return _flood_states(ctx, mesh, [pin], [lay], masks.open, masks.fit, cand_l)

    at_pose = _sides_at_pose(d, pose, _N_LAYERS)
    main = states(0, _pin_layers(at_pose, 0, _N_LAYERS, cand))
    posed = states(1, _pin_layers(at_pose, 1, _N_LAYERS, cand))
    authored = states(1, _pin_layers(d, 1, _N_LAYERS, cand))
    plain = _flood_region(ctx, mesh, [1], masks.base_pass) & _flood_region(
        ctx, mesh, [0], masks.base_pass
    )
    return bool(posed & main), bool(authored & main), bool(plain)


def test_flipped_pad_seeds_on_its_posed_face() -> None:
    d = _board([{"x": 10.0, "y": 20.0, "net": 0}], n_nets=1)
    pose = d.pose0.copy()
    pose[0, 3] = 1.0
    (pad,) = build_mesh(d, pose).pads or ()
    assert pad.side == 1, "the mesh walls the pad on the bottom"
    cand = _cand_mask(frozenset(range(_N_LAYERS)), _N_LAYERS)
    at_pose = _sides_at_pose(d, pose, _N_LAYERS)
    assert _pin_layers(d, 0, _N_LAYERS, cand) == 0b01, "authored: the top"
    assert _pin_layers(at_pose, 0, _N_LAYERS, cand) == 0b10, "posed: the bottom"


def test_flipped_pad_under_foreign_top_copper_escapes_on_the_bottom() -> None:
    posed, authored, plain = _reach(cover_side=0)
    assert plain, "the any-layer flood always met the main tree"
    assert not authored, "seeded on the authored top face it is walled in"
    assert posed, "seeded on its posed bottom face it reaches pin 0"


def test_pad_walled_on_every_face_stays_stranded() -> None:
    posed, _authored, plain = _reach(cover_side=2)
    assert not plain
    assert not posed, "a drilled foreign land over it walls every layer"


def test_sides_at_pose_moves_smd_pads_and_keeps_drilled() -> None:
    d = _board(
        [
            {"x": 10.0, "y": 20.0, "net": 0},
            {"x": 30.0, "y": 20.0, "net": 0},
            {"x": 20.0, "y": 10.0, "net": 0, "side": 2},
        ],
        n_nets=1,
    )
    assert _sides_at_pose(d, d.pose0, _N_LAYERS) is d, "no flip, no copy"
    pose = d.pose0.copy()
    pose[1:, 3] = 1.0
    got = _sides_at_pose(d, pose, _N_LAYERS)
    assert got.pin_side.tolist() == [0, 1, 2]
    assert d.pin_side.tolist() == [0, 0, 2], "the input design is untouched"
    # a net spanning both faces at the pose forces no outer layer
    assert got.net_pin_force.tolist() == [LAYER_FORCE_FREE]
