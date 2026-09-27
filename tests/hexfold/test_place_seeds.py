"""``_place_seeds`` (build.py): the rigid placement of per-instance seeds
along the connect graph puts every fused rim face to face.

Regression for the keying bug found 2026-09-27: the per-instance
transform table was filed under the *destination* instance and read back
as the source's, so each neighbour received the transform computed for the
other side and landed mirrored behind the far rim -- 8-78 A crossing bonds
in every multi-instance example, which ``stick``'s spring stage then
"repaired" by telescoping the halves into each other.  The rim-frame
normal was also signed against the whole-net centroid instead of the
owning instance's (a coin toss for a hole rim near a sheet's centre).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hexfold.build import Net, build
from hexfold.stick import stick

_EXAMPLES = Path(__file__).resolve().parents[2] / "hexfold" / "examples"


def _crossing_lengths(net: Net) -> np.ndarray:
    assert net.seed3 is not None
    pos = np.array(net.seed3, dtype=np.float64)
    inst = [a.instance for a in net.atoms]
    cross = [(i, j) for i, j, _ in net.bonds if inst[i] != inst[j]]
    assert cross, "no inter-instance bonds -- not a fused net"
    return np.array([np.linalg.norm(pos[i] - pos[j]) for i, j in cross])


def _bond_lengths(net: Net, pos: np.ndarray) -> np.ndarray:
    return np.array([np.linalg.norm(pos[i] - pos[j]) for i, j, _ in net.bonds])


@pytest.mark.parametrize(
    "body, skew_deg",
    [
        # tip-to-tip zigzag tubes (hexfold/examples/tube_fuse.hx)
        ("a: tube(5,0, len=3)\nb: tube(5,0, len=3)\na.out --fuse k=0--> b.in", 0.0),
        # a three-instance chain: exercises the transform composition
        (
            "a: tube(12,0, len=3)\nb: tube(12,0, len=3)\nc: tube(12,0, len=3)\n"
            "a.out --fuse k=0--> b.in\nb.out --fuse k=0--> c.in",
            0.0,
        ),
        # phase k != 0
        ("a: tube(6,0, len=3)\nb: tube(6,0, len=3)\na.out --fuse k=2--> b.in", 0.0),
        # armchair: the dangling bonds sit 30 degrees off the axis (SPEC 10,
        # alpha = 60 degrees to the rim line), and _fuse_transform spaces the
        # rim centroids one sigma apart along the normal regardless of rim
        # type, so the seed's crossing bonds are sigma / cos 30 -- a 15%
        # stretch stick's spring stage closes, not a mis-placement.
        ("a: tube(5,5, len=3)\nb: tube(5,5, len=3)\na.out --fuse k=0--> b.in", 30.0),
    ],
)
def test_fused_tube_seeds_meet_at_one_bond_length(body: str, skew_deg: float) -> None:
    net = build("hexfold 0.2\n" + body + "\n")
    expected = net.lattice.sigma_A / np.cos(np.radians(skew_deg))
    lengths = _crossing_lengths(net)
    assert np.all(np.abs(lengths - expected) < 0.06), (expected, lengths)


@pytest.mark.parametrize(
    "name, bound_A",
    [
        # sheet with a tube budded into a hex hole -- the hole-rim normal
        # sign case (whole-net centroid vs instance centroid)
        ("sheet_bud_22.hx", 1.6),
        # tube + lid + tube: three instances, two fuses
        ("pillar.hx", 1.6),
        # flat lid on a tube (dome-seeded cap onto a cylinder rim)
        ("lid_pillbox.hx", 2.0),
    ],
)
def test_example_seeds_have_no_long_crossing_bonds(name: str, bound_A: float) -> None:
    text = (_EXAMPLES / name).read_text(encoding="utf-8")
    net = build(text, strict=False)
    lengths = _crossing_lengths(net)
    assert lengths.max() < bound_A, (name, lengths.min(), lengths.max())


def test_stick_no_longer_telescopes_the_fused_tube() -> None:
    """With the seed straight, stick's spring stage has nothing to fold:
    every bond stays near sigma and the composite spans two tube lengths
    along its axis (the telescoped seed came out half as long)."""
    body = "a: tube(12,0, len=4)\nb: tube(12,0, len=4)\na.out --fuse k=0--> b.in"
    net = build("hexfold 0.2\n" + body + "\n")
    free = build("hexfold 0.2\na: tube(12,0, len=4)\n")
    pos = stick(net)
    bl = _bond_lengths(net, pos)
    assert bl.min() > 1.35 and bl.max() < 1.50, (bl.min(), bl.max())

    def _axial_extent(p: np.ndarray) -> float:
        c = p - p.mean(axis=0)
        axis = np.linalg.svd(c, full_matrices=False)[2][0]
        proj = c @ axis
        return float(proj.max() - proj.min())

    assert _axial_extent(pos) > 1.9 * _axial_extent(stick(free))
