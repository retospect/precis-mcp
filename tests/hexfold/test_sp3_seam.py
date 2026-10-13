"""sp3 seam lines: analytic graphs, angle tables, refusals, ring census."""

from __future__ import annotations

import math
from collections import Counter

import numpy as np
import pytest

from hexfold.check import _findings_from_net
from hexfold.join import Block, _ZigzagSegment
from hexfold.lattice import Lattice
from hexfold.sp3_seam import (
    _armchair_sheet,
    _ArmchairSegment,
    _seam_angles,
    _sheet_residuals,
    compose_sp3,
    straight_k3z,
    straight_k4,
)
from hexfold.y_junction import _sheet

SIGMA = Lattice().sigma_A
PERIOD_Z = math.sqrt(3) * SIGMA
PERIOD_A = 3.0 * SIGMA
SQUASHED = math.degrees(math.acos(-0.25))  # 104.4775 deg
# GFN2-xTB-relaxed seam lengths measured on the equal-120 sp2 Y seam and
# the seam tube (tblite, FIRE fmax 0.05, 2026-10-13): both seams pair
# into dimers along the axis — the k3-sp3-z motif.
DIMER_A = 1.60
SHEET_BOND_A = 1.51
_HDX = (PERIOD_Z - DIMER_A) / 2  # seam atom's axial offset from its foot
_RHO = math.sqrt(SHEET_BOND_A**2 - _HDX**2)  # rim radius off the axis
# seam angles: three sheet-bond/chain-bond + three sheet-bond/sheet-bond
ANG_SC = math.degrees(math.acos(-_HDX / SHEET_BOND_A))  # 106.5353
ANG_SS = math.degrees(math.acos((_HDX**2 - _RHO**2 / 2) / SHEET_BOND_A**2))
# rim atom's in-plane tilt residual: the seam bond leans atan(hdx/rho)
RIM_TILT = math.degrees(math.atan(_HDX / _RHO))  # 16.5353


def _degrees(net):
    return np.bincount(
        [v for i, j, _ in net.bonds for v in (i, j)], minlength=len(net.atoms)
    )


def _rings_are_bond_walks(net):
    edges = {frozenset((i, j)) for i, j, _ in net.bonds}
    for ring in net.rings:
        assert len(set(ring)) == len(ring)
        assert all(
            frozenset((a, b)) in edges for a, b in zip(ring, ring[1:] + ring[:1])
        )


def _seam_census(net, seam):
    seam_set = set(seam)
    return Counter(len(r) for r in net.rings if any(o in seam_set for o in r))


@pytest.mark.parametrize("sheet", [[2, 3], [4, 6], [6, 4]])
def test_k4_analytic_squashed_tetrahedron(sheet: list[int]) -> None:
    net, result = straight_k4(*sheet)
    assert len(result.seam_atoms) == sheet[0]
    for table in _seam_angles(result.coords, net.bonds, result.seam_atoms):
        assert table == pytest.approx([SQUASHED] * 4 + [120.0] * 2, abs=1e-6)
    _rings_are_bond_walks(net)
    deg = _degrees(net)
    for atom in result.seam_atoms:
        assert deg[atom] == 4
        assert net.atoms[atom].element == "C"
        assert net.atoms[atom].hyb == "sp3"
    lengths = [
        np.linalg.norm(result.coords[i] - result.coords[j]) for i, j, _ in net.bonds
    ]
    assert lengths == pytest.approx([SIGMA] * len(lengths), abs=1e-12)
    # 4 sheet-pair channels x (periods - 1) twelve-cycle faces
    assert _seam_census(net, result.seam_atoms) == {12: 4 * (sheet[0] - 1)}
    assert _sheet_residuals(result.sheets, net.bonds, net.rings) == {
        f"sheet{j}": 0 for j in range(4)
    }
    residuals = {
        dict(f.data)["sheet"]: dict(f.data)["residual"]
        for f in _findings_from_net(net)
        if f.code == "euler.residual"
    }
    assert residuals == {f"sheet{j}": 0 for j in range(4)}


def test_k4_deterministic_repeat() -> None:
    net, result = straight_k4(5, 5)
    again, repeat = straight_k4(5, 5)
    assert net == again
    assert np.array_equal(result.coords, repeat.coords)


def test_k4_bonded_rim_atoms_keep_nominal_angles() -> None:
    """The seam bond continues the armchair dangling direction: rim atoms
    read exactly 120 deg at the analytic registration."""
    net, result = straight_k4(4, 5)
    adj: dict[int, list[int]] = {}
    for i, j, _ in net.bonds:
        adj.setdefault(i, []).append(j)
        adj.setdefault(j, []).append(i)
    bonded = {nb for atom in result.seam_atoms for nb in adj[atom]}
    for d in bonded:
        vec = result.coords[adj[d]] - result.coords[d]
        vec /= np.linalg.norm(vec, axis=1)[:, None]
        ang = np.degrees(np.arccos(np.clip(vec @ vec.T, -1.0, 1.0)))
        pairs = [float(ang[i, j]) for i in range(3) for j in range(i + 1, 3)]
        assert pairs == pytest.approx([120.0] * 3, abs=1e-6)


@pytest.mark.parametrize("sheet", [[2, 2], [4, 4], [6, 3]])
def test_k3z_analytic_dimer_chain(sheet: list[int]) -> None:
    net, result = straight_k3z(*sheet)
    n = sheet[0]
    assert len(result.seam_atoms) == n
    for table in _seam_angles(result.coords, net.bonds, result.seam_atoms):
        assert table == pytest.approx([ANG_SC] * 3 + [ANG_SS] * 3, abs=1e-6)
    _rings_are_bond_walks(net)
    deg = _degrees(net)
    for atom in result.seam_atoms:
        assert deg[atom] == 4
        assert net.atoms[atom].hyb == "sp3"
    # chain bonds only between even-odd dimer pairs, at the xTB length
    seam_set = set(result.seam_atoms)
    seam_bonds = [
        (min(i, j), max(i, j))
        for i, j, _ in net.bonds
        if i in seam_set and j in seam_set
    ]
    assert sorted(seam_bonds) == sorted(
        (result.seam_atoms[2 * m], result.seam_atoms[2 * m + 1]) for m in range(n // 2)
    )
    for a, b in seam_bonds:
        assert np.linalg.norm(result.coords[a] - result.coords[b]) == pytest.approx(
            DIMER_A, abs=1e-12
        )
    # seam-sheet bonds at the xTB length; sheet-internal bonds at sigma
    for i, j, _ in net.bonds:
        if i in seam_set and j in seam_set:
            continue
        expected = SHEET_BOND_A if (i in seam_set or j in seam_set) else SIGMA
        assert np.linalg.norm(result.coords[i] - result.coords[j]) == pytest.approx(
            expected, abs=1e-12
        )
    # dimer pentagons x3 sheets + gap octagons x3 sheet pairs
    census = _seam_census(net, result.seam_atoms)
    assert census[5] == 3 * (n // 2)
    assert census[8] == 3 * (n // 2 - 1)
    assert sum(census.values()) == 3 * (n - 1)
    assert _sheet_residuals(result.sheets, net.bonds, net.rings) == {
        f"sheet{j}": 0 for j in range(3)
    }


def test_k3z_deterministic_repeat() -> None:
    net, result = straight_k3z(4, 4)
    again, repeat = straight_k3z(4, 4)
    assert net == again
    assert np.array_equal(result.coords, repeat.coords)


def test_k3z_rim_atoms_carry_the_tilt_strain() -> None:
    """A zigzag dangling bond is perpendicular to the axis; the chain
    geometry tilts the seam bond atan(hdx/rho) = 16.54 deg in-plane,
    which the rim atom reads as 103.46/120/136.54 deg — held by the
    pinning, so the table is the authored seed, not relaxed evidence."""
    net, result = straight_k3z(4, 4)
    adj: dict[int, list[int]] = {}
    for i, j, _ in net.bonds:
        adj.setdefault(i, []).append(j)
        adj.setdefault(j, []).append(i)
    bonded = {nb for atom in result.seam_atoms for nb in adj[atom]}
    bonded -= set(result.seam_atoms)
    for d in bonded:
        vec = result.coords[adj[d]] - result.coords[d]
        vec /= np.linalg.norm(vec, axis=1)[:, None]
        ang = np.degrees(np.arccos(np.clip(vec @ vec.T, -1.0, 1.0)))
        pairs = sorted(float(ang[i, j]) for i in range(3) for j in range(i + 1, 3))
        assert pairs == pytest.approx(
            sorted([120.0 - RIM_TILT, 120.0, 120.0 + RIM_TILT]), abs=1e-6
        )


def test_armchair_segment_shape_and_rejection() -> None:
    block, rim = _armchair_sheet(4, 4)
    assert isinstance(rim, _ArmchairSegment)
    assert len(rim.dangling_a) == len(rim.dangling_b) == 4
    # the rim walk carries every dangling atom in dimer pairs along the
    # bottom edge, closed by guard-column atoms at both ends
    edges = {frozenset((i, j)) for i, j, _ in block.bonds}
    assert len(set(rim.atoms)) == len(rim.atoms)
    assert all(frozenset((a, b)) in edges for a, b in zip(rim.atoms, rim.atoms[1:]))
    pos = {a: i for i, a in enumerate(rim.atoms)}
    assert all(pos[rim.dangling_a[i]] + 1 == pos[rim.dangling_b[i]] for i in range(4))
    # a corrupt walk endpoint or a zigzag segment is a port.mismatch
    zig_block, zig = _sheet(4, 4)
    bad = _ArmchairSegment(
        "rim", rim.atoms, rim.dangling_a, rim.dangling_b, (rim.atoms[0], rim.atoms[0])
    )
    result = compose_sp3(
        (block,) * 4,
        (bad,) * 4,
        seam_type="k4-sp3-90-a",
        dihedrals_deg=(90,) * 4,
    )
    assert [f.code for f in result.findings] == ["port.mismatch"]
    result = compose_sp3(
        (zig_block,) * 4,
        (zig,) * 4,
        seam_type="k4-sp3-90-a",
        dihedrals_deg=(90,) * 4,
    )
    assert [f.code for f in result.findings] == ["port.mismatch"]
    assert isinstance(zig, _ZigzagSegment)


def test_compose_refusal_table() -> None:
    zb, zr = _sheet(4, 4)
    ab, ar = _armchair_sheet(4, 4)
    cases: list[
        tuple[
            str,
            tuple[Block, ...],
            tuple[_ZigzagSegment | _ArmchairSegment, ...],
            tuple[float, ...],
            str,
        ]
    ] = [
        # armchair rims offered to the zigzag row
        ("k3-sp3-z", (ab,) * 3, (ar,) * 3, (120,) * 3, "port.mismatch"),
        # zigzag rims offered to the armchair row
        ("k4-sp3-90-a", (zb,) * 4, (zr,) * 4, (90,) * 4, "port.mismatch"),
        # unequal dihedrals
        ("k3-sp3-z", (zb,) * 3, (zr,) * 3, (110, 120, 130), "fit.unsolvable"),
        # k>=5 / wrong arity
        ("k4-sp3-90-a", (zb,) * 5, (zr,) * 5, (90,) * 5, "fit.unsolvable"),
        # unknown type
        ("k3-sp3-a", (zb,) * 3, (zr,) * 3, (120,) * 3, "fit.unsolvable"),
    ]
    for st, blocks, rims, dh, code in cases:
        result = compose_sp3(blocks, rims, seam_type=st, dihedrals_deg=dh)
        assert [f.code for f in result.findings] == [code], st
        assert result.coords.size == 0


def test_k3z_odd_periods_refuse_before_build() -> None:
    zb, zr = _sheet(5, 4)
    result = compose_sp3(
        (zb,) * 3, (zr,) * 3, seam_type="k3-sp3-z", dihedrals_deg=(120,) * 3
    )
    assert [f.code for f in result.findings] == ["fit.unsolvable"]
    with pytest.raises(ValueError, match="fit.unsolvable"):
        straight_k3z(5, 4)


def test_seam_faces_close_around_every_seam_pair() -> None:
    """Face completeness: every minted seam face is a closed walk on real
    bonds containing exactly two chain-adjacent seam atoms, and every
    adjacent seam-atom pair closes exactly k faces — (periods - 1) per
    sheet-pair channel."""
    for k, built in ((4, straight_k4(4, 4)), (3, straight_k3z(4, 4))):
        net, result = built
        seam_set = set(result.seam_atoms)
        pos = {a: i for i, a in enumerate(result.seam_atoms)}
        edges = {frozenset((i, j)) for i, j, _ in net.bonds}
        faces = [r for r in net.rings if any(o in seam_set for o in r)]
        counts: Counter[tuple[int, int]] = Counter()
        for face in faces:
            assert all(
                frozenset((a, b)) in edges for a, b in zip(face, face[1:] + face[:1])
            )
            sm = sorted(pos[o] for o in face if o in seam_set)
            assert len(sm) == 2 and sm[1] - sm[0] == 1
            counts[(sm[0], sm[1])] += 1
        assert counts == Counter(
            {(i, i + 1): k for i in range(len(result.seam_atoms) - 1)}
        )
        assert len(faces) == k * (len(result.seam_atoms) - 1)


def test_seam_atoms_are_sp3_carbon_degree_four() -> None:
    for net, result in (straight_k4(3, 4), straight_k3z(4, 4)):
        deg = _degrees(net)
        for atom in result.seam_atoms:
            a = net.atoms[atom]
            assert a.element == "C" and a.hyb == "sp3" and deg[atom] == 4
        findings = _findings_from_net(net)
        assert not [f for f in findings if f.severity.name != "INFO"]
