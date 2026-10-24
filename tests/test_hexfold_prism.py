"""Prism tube: flat zigzag wall strips joined at signed equal-120 k3
seams, each seam's third sheet an outward fin or an inward rib; polygon
closure, analytic seed registration, rib clearance, pores."""

from __future__ import annotations

import numpy as np
import pytest

from hexfold.build import Net
from hexfold.check import _clash_pairs, geometry_findings
from hexfold.prism import PrismTube, prism_tube
from hexfold.report import Severity


def _interior_angles(tube: PrismTube) -> list[float]:
    """Interior angle at each polygon vertex, measured on the seed's seam
    lines (the record's ``vertices``)."""
    verts = np.asarray(tube.vertices)
    n = len(verts)
    out = []
    for k in range(n):
        a = verts[(k - 1) % n] - verts[k]
        b = verts[(k + 1) % n] - verts[k]
        signed = np.degrees(np.arctan2(a[0] * b[1] - a[1] * b[0], a @ b))
        out.append(float((-signed) % 360.0))
    return out


def _adjacency(net: Net) -> dict[int, list[int]]:
    adj: dict[int, list[int]] = {}
    for i, j, _ in net.bonds:
        adj.setdefault(i, []).append(j)
        adj.setdefault(j, []).append(i)
    return adj


def test_hex6_builds_clean_and_trivalent() -> None:
    net, tube = prism_tube(8, 6, 4, "++++++")
    again, repeat = prism_tube(8, 6, 4, "++++++")
    assert net == again and np.array_equal(tube.coords, repeat.coords)
    findings = geometry_findings(net)
    assert not [f for f in findings if f.severity == Severity.ERROR]
    sigma = net.lattice.sigma_A
    adj = _adjacency(net)
    pos = tube.coords
    for seam in tube.seam_atoms:
        for s in seam:
            neighbours = adj[s]
            assert len(neighbours) == 3
            vec = pos[neighbours] - pos[s]
            assert np.linalg.norm(vec, axis=1) == pytest.approx([sigma] * 3, abs=1e-9)
            unit = vec / sigma
            assert np.linalg.det(unit) == pytest.approx(0.0, abs=1e-9)
            cos = np.clip(unit @ unit.T, -1, 1)
            ang = np.degrees(np.arccos(cos))
            assert ang[np.triu_indices(3, 1)] == pytest.approx([120.0] * 3, abs=5.0)
    assert _interior_angles(tube) == pytest.approx([120.0] * 6, abs=1.0)
    assert tube.side_A == pytest.approx((1.5 * 5 + 0.5) * sigma + 2 * sigma, abs=1e-9)
    assert tube.radius_A == pytest.approx(tube.side_A, abs=1e-9)
    assert not _clash_pairs(tube.coords, net.bonds, 1.8)
    # same census shape as the rail tube: N sheets, N seams
    n = len(tube.signs)
    n_s = 2 * (8 + 2) * 4
    n_w = 2 * (8 + 2) * 6
    assert len(net.atoms) == n * (n_s + n_w + 8)
    assert sum(len(r) == 8 for r in net.rings) == 3 * n * (8 - 1)
    assert len(net.atoms) - len(net.bonds) + len(net.rings) == n * (8 - 1)
    assert len(tube.pinned) == 4 * n * 8
    assert [j.name for j in net.junctions] == [f"seam:{k}:+" for k in range(6)]


def test_tri12_ribs_point_at_the_axis() -> None:
    net, tube = prism_tube(8, 6, 4, "+++-+++-+++-")
    assert tube.signs == (
        "+",
        "+",
        "+",
        "-",
        "+",
        "+",
        "+",
        "-",
        "+",
        "+",
        "+",
        "-",
    )
    angles = _interior_angles(tube)
    for k, s in enumerate(tube.signs):
        assert angles[k] == pytest.approx(120.0 if s == "+" else 240.0, abs=1.0)
    # rib free edges point at the axis: the rib's authored direction (the
    # seam's third-sheet direction) anti-aligns with the tip's outward
    # radial
    pos = tube.coords
    verts = np.asarray(tube.vertices)
    dirs = np.asarray(tube.strip_dirs)
    for k, s in enumerate(tube.signs):
        if s != "-":
            continue
        xy = pos[list(tube.strip_atoms[k])][:, :2]
        t = (xy - verts[k]) @ dirs[k]  # distance along the rib
        tip = xy[t > t.max() - 0.5].mean(axis=0)
        radial = tip / np.linalg.norm(tip)
        assert dirs[k] @ radial < -0.9


def test_net_turn_mismatch_refused() -> None:
    with pytest.raises(ValueError, match=r"prism\.closure: net turn 180°"):
        prism_tube(8, 6, 4, "++-++-++-")


def test_open_polygon_refused() -> None:
    with pytest.raises(ValueError, match=r"prism\.closure: open polygon"):
        prism_tube(8, 6, 4, "+++++++-")


def test_self_intersecting_polygon_refused() -> None:
    # net turn 360 and a zero gap, but the walk revisits a vertex
    with pytest.raises(ValueError, match=r"prism\.closure: self-intersecting"):
        prism_tube(8, 6, 4, "+++++--+++++--")


@pytest.mark.parametrize(
    "seams",
    ["++", "x+++++", "+", "++x--+++++"],
)
def test_seams_refused_by_name(seams: str) -> None:
    with pytest.raises(ValueError, match=r"prism\.seams"):
        prism_tube(8, 6, 4, seams)


def test_seams_count_bound() -> None:
    with pytest.raises(ValueError, match=r"3\.\.24"):
        prism_tube(8, 6, 4, "+" * 25)


def test_u2212_minus_parses() -> None:
    net, tube = prism_tube(8, 6, 4, "+++−+++−+++−")
    assert tube.signs == (
        "+",
        "+",
        "+",
        "-",
        "+",
        "+",
        "+",
        "-",
        "+",
        "+",
        "+",
        "-",
    )
    assert len(net.atoms) == 2496


def test_odd_wall_rows_refused() -> None:
    with pytest.raises(ValueError, match="must be even"):
        prism_tube(8, 5, 4, "++++++")


def test_count_errors_name_the_builder() -> None:
    """gr514058 — ``sheetnet._count``'s error prefix is the calling
    layout, never the default "seam tube" (only ``fin120.seam_tube``
    keeps that one)."""
    from hexfold.channel import channel_junction
    from hexfold.fin120 import seam_tube
    from hexfold.railtube import rail_tube

    with pytest.raises(ValueError, match=r"^prism tube periods"):
        prism_tube(99, 4, 2, "++++++")
    with pytest.raises(ValueError, match=r"^rail tube periods"):
        rail_tube(99, 4, 2, 3)
    with pytest.raises(ValueError, match=r"^channel periods"):
        channel_junction(99, 4, 3, ("out", "out", "out"))
    with pytest.raises(ValueError, match=r"^seam tube periods"):
        seam_tube(99, 4, 2, "out")


def test_rib_tips_colliding_refused() -> None:
    # two ribs face each other across a skinny waist: tips 1.4 A apart
    with pytest.raises(ValueError, match=r"prism\.rib\.clearance"):
        prism_tube(4, 4, 2, "++++-++++-")
    # and a long rib through the lumen hits the far wall
    with pytest.raises(ValueError, match=r"prism\.rib\.clearance"):
        prism_tube(8, 4, 12, "++++-++++-")


def test_pore_on_a_seam_rim_refused() -> None:
    with pytest.raises(ValueError, match="seam rim"):
        prism_tube(8, 6, 4, "++++++", pores=[(0, 0, 0)])
    with pytest.raises(ValueError, match="pore wall"):
        prism_tube(8, 6, 4, "++++++", pores=[(6, 1, 2)])


def test_pore_removes_one_hexagon_leaving_an_open_rim() -> None:
    net, tube = prism_tube(8, 6, 4, "++++++", pores=[(0, 1, 2), (3, 2, 2)])
    clean, _plain = prism_tube(8, 6, 4, "++++++")
    assert len(net.atoms) == len(clean.atoms) - 12
    assert len(tube.pore_rims) == 2
    regions = dict(net.regions)
    degrees = np.bincount(
        [v for i, j, _ in net.bonds for v in (i, j)], minlength=len(net.atoms)
    )
    for i, rim in enumerate(tube.pore_rims):
        assert len(rim) == 6
        assert regions[f"pore{i}"] == rim
        assert all(degrees[o] == 2 for o in rim)
    assert len(tube.wall_atoms[0]) == len(tube.wall_atoms[1]) - 6
    assert len(tube.wall_atoms[3]) == len(tube.wall_atoms[1]) - 6


def test_regions_match_records() -> None:
    net, tube = prism_tube(6, 6, 3, "++++++")
    regions = dict(net.regions)
    for k in range(6):
        assert regions[f"strip{k}"] == tube.strip_atoms[k]
        assert regions[f"wall{k}"] == tube.wall_atoms[k]
        assert regions[f"seam{k}"] == tube.seam_atoms[k]
    # seam 0 sits on +x: the authored theta-zero meridian
    for s in tube.seam_atoms[0]:
        assert tube.coords[s, 1] == pytest.approx(0.0, abs=1e-9)
        assert tube.coords[s, 0] > 0
