"""Synthetic 100-part board for the topo-place slice-1 tests.

Deterministic per ``seed``: parts on a jittered grid over a square
outline, 2-pin signal nets between random pairs plus a few small
multi-pin nets, two pins per part at ±0.5 mm (small enough that the
pin-level proxy and ``autoplace``'s centroid metric measure the same
board — the comparison test reads the same pose either way). A few
corner parts are fixed so the mover's lock path is exercised too.

``autoplace_args()`` returns the ``instances``/``nets`` rows
:func:`precis.pcb.place.autoplace` already consumes, from the SAME
seed draw, so an equal-eval comparison is one board, not two.
"""

from __future__ import annotations

import numpy as np

from precis.pcb.topoplace.design import TopoDesign

#: default board spec (the tests' "100-part synthetic board")
N_PARTS = 100
N_NETS = 180
N_MULTINET = 12  # nets on 3-4 parts instead of 2
N_FIXED = 4
SIDE_MM = 100.0


def synth_design(
    seed: int = 0,
    n_parts: int = N_PARTS,
    n_nets: int = N_NETS,
    n_multinet: int = N_MULTINET,
    n_fixed: int = N_FIXED,
    side: float = SIDE_MM,
) -> tuple[TopoDesign, list[dict], list[dict]]:
    """Returns ``(design, autoplace_instances, autoplace_nets)``."""
    rng = np.random.default_rng(seed)

    parts = tuple(f"U{i + 1}" for i in range(n_parts))

    # pose0: jittered grid, deterministic
    cols = int(np.ceil(np.sqrt(n_parts)))
    pitch = side / (cols + 1)
    gx, gy = np.meshgrid(pitch * (np.arange(cols) + 1), pitch * (np.arange(cols) + 1))
    grid = np.column_stack([gx.ravel(), gy.ravel()])[:n_parts]
    grid += rng.normal(0.0, pitch * 0.15, grid.shape)
    rot0 = rng.integers(0, 4, n_parts).astype(np.float64)
    side0 = np.zeros(n_parts)
    pose0 = np.column_stack([grid, rot0, side0])

    # pins: two per part at +/-0.5 mm on x (footprint-local)
    pin_xy = np.asarray([[-0.5, 0.0], [0.5, 0.0]] * n_parts)
    pin_part = np.repeat(np.arange(n_parts), 2)
    pin_side = np.zeros(n_parts * 2, dtype=np.int8)

    # nets: n_multinet multi-pin nets (3-4 parts), rest 2-pin pairs —
    # every member is one pin of the part (pin 2*i or 2*i+1).
    net_pins: list[np.ndarray] = []
    nets_rows: list[dict] = []
    busy_pairs: set[tuple[int, int]] = set()
    for ni in range(n_nets):
        if ni < n_multinet:
            k = int(rng.integers(3, 5))
            members = rng.choice(n_parts, size=k, replace=False)
        else:
            while True:
                a, b = sorted(rng.choice(n_parts, size=2, replace=False).tolist())
                if (a, b) not in busy_pairs:
                    busy_pairs.add((a, b))
                    break
            members = np.asarray([a, b])
        pins = np.asarray([2 * int(p) + int(rng.integers(0, 2)) for p in members])
        net_pins.append(pins)
        nets_rows.append(
            {
                "name": f"n{ni}",
                "net_class": "signal",
                "members": [{"refdes": parts[int(p)]} for p in members],
            }
        )

    fixed = np.zeros(n_parts, dtype=bool)
    fixed[:n_fixed] = True
    freedoms = np.full(n_parts, 3, dtype=np.uint8)  # ROTATE | BACK_SIDE
    outline = np.asarray([[0, 0], [side, 0], [side, side], [0, side]])

    # Courtyard squares — hull+margin sized to contain the +/-0.5 pins:
    # without them the mesh tests exercise pads and the outline only
    # (slice-2a seam item 7).
    sq = np.asarray([[-0.9, -0.9], [0.9, -0.9], [0.9, 0.9], [-0.9, 0.9], [-0.9, -0.9]])
    part_outline = tuple(sq.copy() for _ in range(n_parts))

    pin_net = np.full(n_parts * 2, -1, dtype=np.int32)
    for ni, pins in enumerate(net_pins):
        pin_net[pins] = ni

    design = TopoDesign(
        parts=parts,
        pin_xy=pin_xy,
        pin_part=pin_part,
        pin_side=pin_side,
        pin_label=np.asarray([str(i) for i in range(n_parts * 2)], dtype=object),
        pin_net=pin_net,
        net_pins=tuple(net_pins),
        flex=(),
        swap_groups=(),
        fixed=fixed,
        freedoms=freedoms,
        outline=outline,
        pose0=pose0,
        part_outline=part_outline,
    )

    instances = [
        {
            "refdes": parts[i],
            "x": float(pose0[i, 0]),
            "y": float(pose0[i, 1]),
            "fixed": "xy" if fixed[i] else None,
        }
        for i in range(n_parts)
    ]
    return design, instances, nets_rows


__all__ = ["synth_design"]
