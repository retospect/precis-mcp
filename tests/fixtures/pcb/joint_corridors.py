"""Physical, offline routing examples, not asserted missing capabilities.

All routes are F.Cu only. The ABC gate admits widths 0.60/0.40/0.20
through 0.95/0.75/0.55 mm openings at 0.15 mm clearance. The simultaneous
witness uses A1/B2/C3; B1/C2 are possible incumbent choices, but these
fixtures do not inject a controlled incumbent into the search.
"""

from __future__ import annotations

from dataclasses import dataclass

import precis_geom.pcb as pcb
from precis.pcb import local
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import design_from_ir
from precis.pcb.topoplace.realise import RoutePiece, RoutePlan


@dataclass(frozen=True)
class CorridorFixture:
    name: str
    board: pcb.Board
    widths: dict[str, float]
    witness: dict[str, tuple[tuple[float, float], ...]]
    walls: tuple[tuple[tuple[float, float], ...], ...]

    def inputs(self):
        design = local._design(self.board, None)
        ir = local._build_ir(design)
        footprints = local._footprints(design, ir)
        td = design_from_ir(
            ir,
            footprints,
            net_classes=design.graph.get("net_classes"),
            features=design.features,
            route_planes=True,
        )
        rules = {
            name: NetRules(track_width_mm=width, clearance_mm=0.15)
            for name, width in self.widths.items()
        }
        pieces = tuple(
            RoutePiece(k, k, 0, self.witness[name])
            for k, name in enumerate(td.net_name)
            if name in self.witness
        )
        return (
            td,
            ir,
            footprints,
            rules,
            RoutePlan(pieces=pieces, vias=(), layers=(0,) * len(pieces)),
        )


def _rect(x0, y0, x1, y1):
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def _fixture(name, rows, gaps, *, divider=False):
    board = pcb.Board(name, stackup=2, outline=pcb.rect(30.0, 15.0))
    fp = board.footprint(
        pcb.Footprint("DOT", [pcb.pad("1", "rect", 0.0, 0.0, w=0.9, h=0.9)])
    )
    widths = {}
    witness: dict[str, tuple[tuple[float, float], ...]] = {}
    for net, y, width in rows:
        board.net_class(net, layers=["F.Cu"])
        ends = []
        for suffix, x in (("L", 3.0), ("R", 27.0)):
            part = board.part(net + suffix, footprint=fp, pins=["1"]).at(x, y)
            part["1"].etype("bidi", fmax_hz=0.0, assume="offline fixture terminal")
            ends.append(part["1"])
        board.net(net, current=0.001, cls=net).tie(*ends)
        widths[net] = width
        witness[net] = ((3.0, y), (27.0, y))
    walls = []
    last = 0.0
    for y, width in gaps:
        walls.append(_rect(14.0, last, 16.0, y - width / 2))
        last = y + width / 2
    walls.append(_rect(14.0, last, 16.0, 11.0 if divider else 15.0))
    if divider:
        walls.append(_rect(0.0, 11.0, 30.0, 12.0))
    for wall in walls:
        board.keepout([list(p) for p in wall])
    return CorridorFixture(name, board, widths, witness, tuple(walls))


def abc_chain():
    """Three unequal lanes plus D behind an impermeable divider."""
    return _fixture(
        "abc-chain",
        (("A", 3.0, 0.6), ("B", 6.0, 0.4), ("C", 9.0, 0.2), ("D", 13.5, 0.2)),
        ((3.0, 0.95), (6.0, 0.75), (9.0, 0.55)),
        divider=True,
    )


def parallel_lanes(*, overfull=False):
    """A centred first 0.20 mm trace prevents a second in a 1 mm gate.

    Two traces need 0.85 mm including all three clearance bands; the
    0.50 mm control is below that requirement; no two-trace witness is
    claimed. The router's fabrication reserve may also reject one there.
    Endpoint pads are 2 mm apart and fan into the bottleneck: the initial
    construction incorrectly put 0.9 mm pads at 0.4 mm pitch, and failed
    physical validation before its baseline was run.
    """
    fixture = _fixture(
        "parallel-overfull" if overfull else "parallel-lanes",
        (("A", 4.0, 0.2), ("B", 6.0, 0.2)),
        ((5.0, 0.5 if overfull else 1.0),),
    )
    fixture.witness.update(
        {
            "A": ((3.0, 4.0), (12.0, 4.8), (18.0, 4.8), (27.0, 4.0)),
            "B": ((3.0, 6.0), (12.0, 5.2), (18.0, 5.2), (27.0, 6.0)),
        }
    )
    return fixture
