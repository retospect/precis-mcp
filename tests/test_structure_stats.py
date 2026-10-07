"""view='stats' — bond / angle / POAV1 statistics and the TOC head line."""

from __future__ import annotations

import json

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis.structure import Atom, Bond, Cell, Scene, geometry_stats

_L = 20.0
_MID = np.array([0.5, 0.5, 0.5])


def _cell() -> Cell:
    return Cell.from_lengths_angles(_L, _L, _L, pbc=(False, False, False))


def _scene(nbrs: list[np.ndarray]) -> Scene:
    sc = Scene(cell=_cell())
    sc.atoms["aC1"] = Atom("aC1", "C", _MID.copy())
    for n, v in enumerate(nbrs, start=2):
        lab = f"aC{n}"
        sc.atoms[lab] = Atom(lab, "C", _MID + np.asarray(v) / _L)
        sc.bonds.append(Bond("aC1", lab))
    return sc


def _planar(r: float = 1.40) -> list[np.ndarray]:
    return [
        np.array([r * np.cos(t), r * np.sin(t), 0.0]) for t in np.radians([0, 120, 240])
    ]


def _tetra(r: float = 1.54) -> list[np.ndarray]:
    dirs = [(1, 1, 1), (1, -1, -1), (-1, 1, -1)]
    return [r * np.array(d) / np.sqrt(3) for d in dirs]


def test_planar_centre_has_zero_pyramidalization_and_known_lengths():
    st = geometry_stats.compute(_scene(_planar()))
    cc = st.bonds[("C", "C")]
    assert cc.count == 3
    assert cc.mean == pytest.approx(1.40, abs=1e-9)
    assert cc.min == pytest.approx(1.40) and cc.max == pytest.approx(1.40)
    assert cc.std == pytest.approx(0.0, abs=1e-9)
    assert st.poav is not None
    assert st.poav.count == 1 and st.poav.max == pytest.approx(0.0, abs=1e-6)
    ang = st.angles[("C", "acyclic")]
    assert ang.count == 3 and ang.mean == pytest.approx(120.0, abs=1e-6)


def test_tetrahedral_centre_is_sp3_pyramidalized():
    st = geometry_stats.compute(_scene(_tetra()))
    assert st.poav is not None
    assert st.poav.mean == pytest.approx(19.47, abs=0.05)
    assert st.poav.above_15 == 1 and st.poav.above_c60 == 1
    assert st.angles[("C", "acyclic")].mean == pytest.approx(109.47, abs=0.05)


def test_min_max_labels_and_extrema():
    v = [
        np.array([1.30, 0, 0]),
        np.array([-0.75, 1.30, 0]),
        np.array([-0.75, -1.50, 0]),
    ]
    cc = geometry_stats.compute(_scene(v)).bonds[("C", "C")]
    assert cc.min == pytest.approx(1.30) and cc.min_at == ("aC1", "aC2")
    assert cc.max == pytest.approx(np.hypot(0.75, 1.50))
    assert cc.max_at == ("aC1", "aC4")


def test_ring_split_uses_smallest_ring():
    sc = Scene(cell=_cell())
    for k in range(6):
        t = np.radians(60 * k)
        off = np.array([1.4 * np.cos(t), 1.4 * np.sin(t), 0.0])
        sc.atoms[f"aC{k + 1}"] = Atom(f"aC{k + 1}", "C", _MID + off / _L)
    for k in range(6):
        sc.bonds.append(Bond(f"aC{k + 1}", f"aC{(k + 1) % 6 + 1}"))
    st = geometry_stats.compute(sc)
    assert st.angles[("C", "6-ring")].count == 6
    assert st.angles[("C", "6-ring")].mean == pytest.approx(120.0, abs=1e-6)


def test_render_has_reference_tier_and_labels():
    out = geometry_stats.render(
        geometry_stats.compute(_scene(_planar())), tier="unknown"
    )
    assert "tier: unknown" in out
    assert "C60 11.6" in out and "1.42" in out and "sp3 19.47" in out
    assert "aC1-aC2" in out and "C–C" in out


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


def _put(structure, slug):
    ops: list[dict] = [{"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]}]
    for v in _planar():
        ops.append(
            {
                "op": "add_atom",
                "element": "C",
                "frac": [float(x) for x in _MID + v / _L],
            }
        )
    for n in (2, 3, 4):
        ops.append({"op": "add_bond", "i": "aC1", "j": f"aC{n}", "order": 1})
    cell = {"a": _L, "b": _L, "c": _L, "pbc": [False, False, False]}
    structure.put(id=slug, text=json.dumps({"cell": cell, "ops": ops}))


def test_stats_view_and_toc_head(structure):
    _put(structure, "star")
    body = structure.get(id="star", view="stats").body
    assert "tier: unknown" in body and "C–C | 3 | 1.400" in body
    assert "n 1 · mean 0.00°" in body
    head = structure.get(id="star").body
    assert (
        "# geometry: C–C 1.400 ± 0.000 (1.400..1.400) Å · "
        "θp mean/p95/max 0.0/0.0/0.0° · tier: unknown"
    ) in head
