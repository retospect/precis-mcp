"""Direct tests for hexfold.channel — the paired-seam channel junction."""

from __future__ import annotations

from collections import Counter

import numpy as np
import pytest

from hexfold.channel import channel_junction


def _degrees(net) -> np.ndarray:
    deg = np.zeros(len(net.atoms), dtype=int)
    for i, j, _ in net.bonds:
        deg[i] += 1
        deg[j] += 1
    return deg


def _rings_are_bond_walks(net) -> None:
    edges = {frozenset((i, j)) for i, j, _ in net.bonds}
    for ring in net.rings:
        for a, b in zip(ring, ring[1:] + ring[:1]):
            assert frozenset((a, b)) in edges, (ring, a, b)


def test_channel_junction_paired_topology() -> None:
    net, cj = channel_junction(6, 4, 3, ("out", "out", "out"))
    # 3 sheets x (strip 4-row-pair + arc 4-row-pair) + 3 seams x 6 atoms
    assert len(net.atoms) == 354
    _rings_are_bond_walks(net)
    deg = _degrees(net)
    for seam in cj.seam_atoms:
        assert len(seam) == 6
        for atom in seam:
            assert net.atoms[atom].hyb == "sp3"
            assert deg[atom] == 4
            # paired chain: one seam-seam bond per atom at the dimer length
            jn = [
                (j if i == atom else i)
                for i, j, _ in net.bonds
                if (i == atom or j == atom) and i in seam and j in seam
            ]
            assert len(jn) == 1
            d = np.linalg.norm(cj.coords[atom] - cj.coords[jn[0]])
            assert d == pytest.approx(1.60, abs=1e-9)
    # per seam the _seam_k3 faces: n-1 octagons x 3 rim-pairs... minted
    census = Counter(len(r) for r in net.rings)
    # sheet hexagons: strip (2*(periods-1)*(rows-1)) + arc same, per block
    assert census == {6: 105, 8: 45}


def test_channel_junction_outward_registration_is_equal120() -> None:
    net, cj = channel_junction(6, 4, 3, ("out", "out", "out"))
    for seam in cj.seam_atoms:
        atom = seam[0]
        nb = [
            (j if i == atom else i) for i, j, _ in net.bonds if i == atom or j == atom
        ]
        assert len(nb) == 4
        v = cj.coords[np.asarray(nb)] - cj.coords[atom]
        v /= np.linalg.norm(v, axis=1)[:, None]
        angles = sorted(
            float(np.degrees(np.arccos(np.clip(v[i] @ v[j], -1, 1))))
            for i in range(4)
            for j in range(i + 1, 4)
        )
        # the authored seam atom is the k3-sp3-z flattened tetrahedron:
        # three ~106.5 deg bond angles and three ~112.2
        assert angles[0] == pytest.approx(106.5, abs=1.0)
        assert angles[-1] == pytest.approx(112.3, abs=1.0)
        assert sum(angles) == pytest.approx(655.2, abs=2.0)


def test_channel_junction_mixed_sides_and_determinism() -> None:
    net1, cj1 = channel_junction(6, 10, 3, ("in", "out", "out"))
    net2, cj2 = channel_junction(6, 10, 3, ("in", "out", "out"))
    assert len(net1.atoms) == len(net2.atoms) == 642
    assert net1.bonds == net2.bonds
    np.testing.assert_allclose(cj1.coords, cj2.coords)
    # inward seam sits inside the wall circle, outward outside
    assert cj1.seam_radius_A[0] < cj1.radius_A
    assert cj1.seam_radius_A[1] > cj1.radius_A
    # the seam faces do not depend on the wall rows
    census = Counter(len(r) for r in net1.rings)
    assert census[8] == 45


def test_channel_junction_inward_tip_clearance() -> None:
    # an inward strip on a small channel would reach inside 2 A of the
    # axis — refused like railtube's inward fin
    with pytest.raises(ValueError, match="does not fit"):
        channel_junction(6, 8, 3, ("in", "out", "out"))
    # a wall big enough seats it
    channel_junction(6, 10, 3, ("in", "out", "out"))


@pytest.mark.parametrize(
    "periods,rows,sides,paired,needle",
    [
        (5, 4, ("out", "out", "out"), True, "even period count"),
        (6, 3, ("out", "out", "out"), True, "wall rows"),
        (6, 4, ("up", "out", "out"), True, "'in' or 'out'"),
        (6, 4, ("out",), True, "2-6"),
        (6, 4, ("out", "out", "out", "out", "out", "out", "out"), True, "2-6"),
        (2, 4, ("in", "in"), True, "does not fit"),
    ],
)
def test_channel_junction_refuses_bad_params(periods, rows, sides, paired, needle):
    with pytest.raises(ValueError, match=needle):
        channel_junction(periods, rows, 3, sides, paired=paired)


def test_channel_junction_unpaired_is_plain_sp2() -> None:
    net, cj = channel_junction(6, 4, 3, ("out", "out", "out"), paired=False)
    deg = _degrees(net)
    for seam in cj.seam_atoms:
        for atom in seam:
            assert net.atoms[atom].hyb == "sp2"
            assert deg[atom] == 3
