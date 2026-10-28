"""Multi-rail seam tube: N arcs on equal-120 seams with inward strips,
authored pores and the analytic seed; fin120 N=2 correspondence."""

from __future__ import annotations

import numpy as np
import pytest

from hexfold.check import _clash_pairs
from hexfold.fin120 import seam_tube
from hexfold.railtube import rail_radius, rail_tube
from hexfold.y_junction import _sheet


def test_rail_radius_spans_the_angular_gap() -> None:
    sigma = 1.42
    for rows, rails in ((4, 2), (10, 3), (18, 4), (40, 6)):
        r, theta0 = rail_radius(rows, sigma, rails)
        y_max = (1.5 * (rows - 1) + 0.5) * sigma
        # dangling atoms at the 120-degree positions, sheet height spanning
        # the 2*pi/N gap between them
        assert r * np.sin(theta0) == pytest.approx(0.5 * np.sqrt(3) * sigma, abs=1e-9)
        assert (2.0 * np.pi / rails - 2.0 * theta0) * r == pytest.approx(
            y_max, abs=1e-9
        )


def test_two_rails_recreate_fin120_inward_up_to_second_strip() -> None:
    """With N=2 the far-side fuse becomes a second seam and strip: every
    fin120 atom/bond has its rail counterpart, the only differences being
    the second strip/seam line and the segment-end guard atoms a fuse
    covered but a k3 segment does not."""
    periods, rows, strip_rows = 6, 18, 3
    net1, tube1 = seam_tube(periods, rows, strip_rows, "in")
    net2, tube2 = rail_tube(periods, rows, strip_rows, 2)
    n_s = len(tube1.strip_atoms)
    n_w = len(tube1.wall_a_atoms)
    assert len(net2.atoms) == len(net1.atoms) + n_s + periods

    def m(o: int) -> int:
        # strip_1 sits in slot 2: everything past fin120's strip shifts n_s
        return o if o < n_s else o + n_s

    fuse = {frozenset(p) for p in tube1.fuse_pairs}
    mapped = {
        frozenset((m(i), m(j)))
        for i, j, _ in net1.bonds
        if frozenset((i, j)) not in fuse
    }
    ours = {frozenset((i, j)) for i, j, _ in net2.bonds}
    # fin120's one seam carries wb's bottom rim; in the rail tube seam_1
    # does, so exactly `periods` bonds move to the second seam line
    y0, y1 = tube2.seam_atoms
    missing = mapped - ours
    assert len(missing) == periods
    for pair in missing:
        s, a = max(pair), min(pair)
        assert s in y0
        assert {a, y1[y0.index(s)]} in ours
    sheet = _sheet(periods, strip_rows)[0]
    assert len(net2.bonds) == (
        len(net1.bonds) - len(tube1.fuse_pairs) + len(sheet.bonds) + 3 * periods
    )
    fuse_rings = {frozenset(r) for r in tube1.fuse_rings}
    mrings = {
        frozenset(m(a) for a in r) for r in net1.rings if frozenset(r) not in fuse_rings
    }
    orings = {frozenset(r) for r in net2.rings}
    assert len(mrings - orings) == 2 * (periods - 1)  # arc-arc eights on seam_1
    assert len(net2.rings) == (
        len(net1.rings) - len(tube1.fuse_rings) + len(sheet.rings) + 3 * (periods - 1)
    )
    d1 = np.bincount(
        [v for i, j, _ in net1.bonds for v in (i, j)], minlength=len(net1.atoms)
    )
    d2 = np.bincount(
        [v for i, j, _ in net2.bonds for v in (i, j)], minlength=len(net2.atoms)
    )
    # the two top-rim guard atoms per wall half lose the fuse's coverage:
    # each has one degree less, every other degree is identical
    top_stubs = {
        off + ord_
        for off in (n_s, n_s + n_w)
        for ord_ in (
            (rows - 1) * (periods + 2) * 2 + 1,  # (rows-1,-1,1)
            ((rows - 1) * (periods + 2) + periods + 1) * 2 + 1,  # (rows-1,periods,1)
        )
    }
    mismatched = {o for o in range(len(net1.atoms)) if d1[o] != d2[m(o)]}
    assert mismatched == top_stubs
    assert all(d2[m(o)] == d1[o] - 1 for o in mismatched)


@pytest.mark.parametrize("rails,rows", [(2, 14), (3, 10), (4, 10), (6, 10)])
def test_rail_tube_graph_census(rails: int, rows: int) -> None:
    periods, strip_rows = 6, 3
    net, tube = rail_tube(periods, rows, strip_rows, rails)
    again, repeat = rail_tube(periods, rows, strip_rows, rails)
    assert net == again and np.array_equal(tube.coords, repeat.coords)
    sigma = net.lattice.sigma_A
    n_s = 2 * (periods + 2) * strip_rows
    n_a = 2 * (periods + 2) * rows
    assert len(net.atoms) == rails * (n_s + n_a + periods)
    assert [len(s) for s in tube.seam_atoms] == [periods] * rails
    assert sum(len(r) == 8 for r in net.rings) == 3 * rails * (periods - 1)
    edges = {frozenset((i, j)) for i, j, _ in net.bonds}
    for ring in net.rings:
        assert len(set(ring)) == len(ring)
        assert all(
            frozenset((a, b)) in edges for a, b in zip(ring, ring[1:] + ring[:1])
        )
    degrees = np.bincount(
        [v for i, j, _ in net.bonds for v in (i, j)], minlength=len(net.atoms)
    )
    assert all(degrees[s] == 3 for seam in tube.seam_atoms for s in seam)
    # V-E+F = rails*(periods-1): 2N sheet disks (chi=1) plus N seams at -3
    assert len(net.atoms) - len(net.bonds) + len(net.rings) == rails * (periods - 1)
    assert len(tube.pinned) == 4 * rails * periods
    assert not _clash_pairs(tube.coords, net.bonds, 1.8)
    # seam atoms at sigma with three 120-degree coplanar bonds, before relax
    adj: dict[int, list[int]] = {}
    for i, j, _ in net.bonds:
        adj.setdefault(i, []).append(j)
        adj.setdefault(j, []).append(i)
    pos = tube.coords
    for k, seam in enumerate(tube.seam_atoms):
        phi = 2.0 * np.pi * k / rails
        for s in seam:
            neighbours = adj[s]
            vec = pos[neighbours] - pos[s]
            assert np.linalg.norm(vec, axis=1) == pytest.approx([sigma] * 3, abs=1e-9)
            unit = vec / sigma
            assert np.linalg.det(unit) == pytest.approx(0.0, abs=1e-12)
            cos = np.clip(unit @ unit.T, -1, 1)
            assert cos[np.triu_indices(3, 1)] == pytest.approx([-0.5] * 3, abs=1e-12)
            assert pos[s, 0] == pytest.approx(
                tube.seam_radius_A * np.cos(phi), abs=1e-9
            )
            assert pos[s, 1] == pytest.approx(
                tube.seam_radius_A * np.sin(phi), abs=1e-9
            )
    regions = dict(net.regions)
    for k in range(rails):
        assert regions[f"strip{k}"] == tube.strip_atoms[k]
        assert regions[f"arc{k}"] == tube.arc_atoms[k]
        assert regions[f"seam{k}"] == tube.seam_atoms[k]


def test_pore_removes_one_hexagon_leaving_an_open_rim() -> None:
    net, tube = rail_tube(6, 10, 3, 3, pores=[(0, 2, 4), (1, 1, 5)])
    clean, _plain = rail_tube(6, 10, 3, 3)
    assert len(net.atoms) == len(clean.atoms) - 12
    assert len(tube.pore_rims) == 2
    regions = dict(net.regions)
    degrees = np.bincount(
        [v for i, j, _ in net.bonds for v in (i, j)], minlength=len(net.atoms)
    )
    adj: dict[int, set[int]] = {}
    for i, j, _ in net.bonds:
        adj.setdefault(i, set()).add(j)
        adj.setdefault(j, set()).add(i)
    for i, rim in enumerate(tube.pore_rims):
        # the six open-valence atoms, degree 2 (each gets one H cap), on an
        # 18-atom boundary walk through the destroyed rings
        assert len(rim) == 6
        assert regions[f"pore{i}"] == rim
        assert all(degrees[o] == 2 for o in rim)
        boundary = {x for o in rim for x in adj[o]} - set(rim)
        assert len(boundary) == 12  # the degree-3 atoms completing the walk
    # each pore costs one face in the Euler census
    assert len(net.atoms) - len(net.bonds) + len(net.rings) == 3 * 5 - 2
    # the removed cells' atoms are gone from the affected arcs only
    assert len(tube.arc_atoms[0]) == len(tube.arc_atoms[2]) - 6
    assert len(tube.arc_atoms[1]) == len(tube.arc_atoms[2]) - 6


@pytest.mark.parametrize(
    "pores",
    [
        [(0, 0, 0)],  # below the allowed row band
        [(0, 0, 1)],  # one row off the bottom rim
        [(0, 0, 7)],  # one row off the top rim (rows-3)
        [(0, 0, 8)],  # top rim row
        [(0, -1, 4)],  # left guard column
        [(0, 5, 4)],  # right guard column (periods-1)
        [(3, 2, 4)],  # arc out of range
        [(0, 2, 4), (0, 3, 4)],  # neighbouring cells share atoms
        [(0, 2, 4), (0, 2, 4)],  # the same cell twice
        [(0, 0, 5), (0, 2, 5)],  # disjoint rings sharing a rim atom
        [(0, 0, 4), (0, 2, 4)],  # the same, one ring higher
    ],
)
def test_pores_touching_rims_or_each_other_refuse(
    pores: list[tuple[int, int, int]],
) -> None:
    with pytest.raises(ValueError, match="pore|arc|cell"):
        rail_tube(6, 10, 3, 3, pores=pores)


def test_pores_two_rings_apart_are_admitted() -> None:
    net, tube = rail_tube(6, 10, 3, 3, pores=[(0, 0, 5), (0, 4, 5)])
    assert len(tube.pore_rims) == 2


def test_inward_strip_reaching_the_axis_is_refused_naming_the_wall() -> None:
    with pytest.raises(
        ValueError, match=r"smallest wall that seats it is \d+ row pairs"
    ):
        rail_tube(6, 10, 4, 3)


def test_wall_suggestion_capped_at_the_row_limit() -> None:
    with pytest.raises(ValueError, match="no wall under 40 row pairs"):
        rail_tube(6, 40, 12, 2)


def test_odd_wall_rows_refused_out_of_phase() -> None:
    with pytest.raises(ValueError, match="must be even"):
        rail_tube(6, 9, 3, 3)
