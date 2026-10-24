"""sanity — the pcb-unified-place-route acceptance board as a workspace
script (``src/precis/pcb/sanity_board.py`` builds the same board straight
into the IR for the router gate; this is its construction through
``precis_geom.pcb``).

Four fixed 10-pin SMT headers centred 25 mm in from each edge of a
100 mm square, four routable layers, 20 two-pin nets: ``TBnn`` pairs the
top and bottom headers pin-for-pin, ``LRnn`` the left and right ones.
``build(tb_seed=, lr_seed=)`` permutes the second header's pin order with
``random.Random(seed)`` (the same permutation ``sanity_board.permutation``
uses), ``free=`` leaves refdes unpinned for the placer.
"""

from __future__ import annotations

import random

import precis_geom.pcb as pcb

BOARD_MM = 100.0
INSET_MM = 25.0
PINS = 10
PITCH_MM = 2.54
LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def permutation(seed: int | None) -> list[int]:
    pins = list(range(1, PINS + 1))
    if seed is not None:
        random.Random(seed).shuffle(pins)
    return pins


def header_footprint() -> pcb.Footprint:
    """One row of ``PINS`` SMT pads along local x, centred on the origin,
    long axis along local y (pointing out of the board edge)."""
    return pcb.Footprint(
        "HDR-SMT-1x10",
        [
            pcb.pad(
                f"P{i + 1}",
                "rect",
                round((i - (PINS - 1) / 2.0) * PITCH_MM, 4),
                0.0,
                w=1.5,
                h=3.0,
            )
            for i in range(PINS)
        ],
    )


def build(
    *,
    tb_seed: int | None = None,
    lr_seed: int | None = None,
    free: tuple[str, ...] = (),
) -> pcb.Board:
    b = pcb.Board(
        "sanity",
        stackup=LAYERS,
        outline=[(0.0, 0.0), (BOARD_MM, 0.0), (BOARD_MM, BOARD_MM), (0.0, BOARD_MM)],
    )
    fp = b.footprint(header_footprint())
    mid, far = BOARD_MM / 2.0, BOARD_MM - INSET_MM
    headers = {}
    for refdes, x, y, rot in (
        ("J_T", mid, far, 0.0),
        ("J_B", mid, INSET_MM, 0.0),
        ("J_L", INSET_MM, mid, 90.0),
        ("J_R", far, mid, 90.0),
    ):
        part = b.part(refdes, label="HDR-SMT-1x10", footprint=fp)
        if refdes not in free:
            part.at(x, y, rot=rot, fixed="both")
        headers[refdes] = part
    for prefix, a, c, seed in (
        ("TB", "J_T", "J_B", tb_seed),
        ("LR", "J_L", "J_R", lr_seed),
    ):
        for i, pin_c in enumerate(permutation(seed), start=1):
            b.wire(
                headers[a][f"P{i}"], headers[c][f"P{pin_c}"], name=f"{prefix}{i:02d}"
            )
    return b


if __name__ == "__main__":
    pcb.run(build(), "out")
