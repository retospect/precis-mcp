"""Spec §B5 example A — the K3 utility instance the slice-2 tests quote.

Three two-pin nets whose straight airwires pairwise cross:

- net A: (0, 2)–(4, 2)
- net B: (2, 0)–(2, 4)
- net C: (0, 0)–(4, 3)

Six single-pin parts, each a 0.8 mm courtyard square centred on its pin,
on a (−2,−2)–(6,6) outline. ``pin_side`` defaults to all-front SMD;
pass ``2`` for the all-TH variant (the §B5 row that makes vias free).
"""

from __future__ import annotations

import numpy as np

from precis.pcb.topoplace.design import TopoDesign

#: net -> (endpoint A, endpoint B) in mm — the spec's literal K3 anchors.
NETS: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {
    "A": ((0.0, 2.0), (4.0, 2.0)),
    "B": ((2.0, 0.0), (2.0, 4.0)),
    "C": ((0.0, 0.0), (4.0, 3.0)),
}

_COURTYARD_HALF = 0.4


def example_a(pin_side: int = 0) -> TopoDesign:
    """The K3 board as a :class:`TopoDesign`; ``pin_side`` is the
    ``TopoDesign.pin_side`` code applied to all six pins (0 front SMD,
    1 back SMD, 2 through-hole)."""
    parts: list[str] = []
    pose0: list[list[float]] = []
    pin_xy = [[0.0, 0.0]]
    pin_part: list[int] = []
    pin_net: list[int] = []
    net_pins: list[list[int]] = []
    outline_local = np.asarray(
        [
            [-_COURTYARD_HALF, -_COURTYARD_HALF],
            [_COURTYARD_HALF, -_COURTYARD_HALF],
            [_COURTYARD_HALF, _COURTYARD_HALF],
            [-_COURTYARD_HALF, _COURTYARD_HALF],
            [-_COURTYARD_HALF, -_COURTYARD_HALF],
        ]
    )
    part_outline: list[np.ndarray] = []
    net_name: list[str] = []
    for ni, (name, (pa, pb)) in enumerate(NETS.items()):
        endpoints: list[int] = []
        for end, xy in enumerate((pa, pb)):
            part = len(parts)
            parts.append(f"{name}{end + 1}")
            pose0.append([xy[0], xy[1], 0.0, 0.0])
            pin_part.append(part)
            pin_net.append(ni)
            part_outline.append(outline_local.copy())
            endpoints.append(len(pin_part) - 1)
        net_pins.append(endpoints)
        net_name.append(name)
    n = len(parts)
    return TopoDesign(
        parts=tuple(parts),
        pin_xy=np.asarray(pin_xy * n, dtype=np.float64),
        pin_part=np.asarray(pin_part, dtype=np.int64),
        pin_side=np.full(len(pin_part), pin_side, dtype=np.int8),
        pin_label=np.asarray(["1"] * len(pin_part), dtype=object),
        pin_net=np.asarray(pin_net, dtype=np.int32),
        net_pins=tuple(np.asarray(p, dtype=np.int64) for p in net_pins),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(n, dtype=bool),
        freedoms=np.full(n, 3, dtype=np.uint8),
        outline=np.asarray([[-2.0, -2.0], [6.0, -2.0], [6.0, 6.0], [-2.0, 6.0]]),
        pose0=np.asarray(pose0, dtype=np.float64),
        part_outline=tuple(part_outline),
        net_layer_force=np.full(len(net_pins), -1, dtype=np.int8),
        net_name=tuple(net_name),
        pin_wh=np.tile(np.asarray([[0.6, 0.6]]), (len(pin_part), 1)),
    )


__all__ = ["NETS", "example_a"]
