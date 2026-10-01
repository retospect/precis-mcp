"""Numba pair kernels vs the pure-Python reference they replaced.

The ``_ref_*`` functions are verbatim copies of the pre-kernel implementations
(``Cell.mic``, ``probe.detect_bonds``/``coordination``, ``validate`` overlap,
``invariants._min_dist``, ``relax._relax_clean`` pair sweep,
``georelax.relax_graph``); the kernels must agree to float rounding.
"""

from __future__ import annotations

import importlib
import itertools
import math

import numpy as np
import pytest

from precis.structure import elements, invariants, probe
from precis.structure.cell import Cell, ImageOffset
from precis.structure.georelax import (
    RELAX_ITERS,
    GeoRelaxTrace,
    angle_theta_gradients,
    angle_triples,
    relax_graph,
)
from precis.structure.relax import _relax_clean
from precis.structure.scene import Atom, Scene

validate = importlib.import_module("precis.structure.validate")

TOL = 1e-9
ELS = ["C", "H", "O", "N", "Pd", "Cu", "Xx"]  # Xx = unknown → default radius


# -- references (verbatim old code) -----------------------------------------


def _ref_mic(cell: Cell, frac_i, frac_j) -> tuple[float, ImageOffset]:
    d0 = np.asarray(frac_j, dtype=float) - np.asarray(frac_i, dtype=float)
    img_base = np.zeros(3, dtype=int)
    for ax in range(3):
        if cell.pbc[ax]:
            img_base[ax] = -int(np.round(d0[ax]))
    ranges = [(-1, 0, 1) if cell.pbc[ax] else (0,) for ax in range(3)]
    best_d2 = np.inf
    best_img: ImageOffset = (0, 0, 0)
    for na, nb, nc in itertools.product(*ranges):
        img = img_base + np.array([na, nb, nc], dtype=int)
        cart = (d0 + img) @ cell.lattice
        d2 = float(cart @ cart)
        if d2 < best_d2:
            best_d2 = d2
            best_img = (int(img[0]), int(img[1]), int(img[2]))
    return float(np.sqrt(best_d2)), best_img


def _ref_detect(scene: Scene, tol: float = 1.2):
    out = []
    labels = list(scene.atoms)
    for ai in range(len(labels)):
        a = scene.atoms[labels[ai]]
        for bj in range(ai + 1, len(labels)):
            b = scene.atoms[labels[bj]]
            dist, img = _ref_mic(scene.cell, a.frac, b.frac)
            if dist <= elements.bond_cutoff(a.element, b.element, tol):
                out.append((a.label, b.label, img))
    return out


def _ref_coord(scene: Scene, label: str, tol: float = 1.2, covalent=False) -> int:
    a = scene.atoms[label]
    n = 0
    for other in scene.atoms.values():
        if other.label == label:
            continue
        if covalent and elements.max_valence(other.element) is None:
            continue
        dist, _ = _ref_mic(scene.cell, a.frac, other.frac)
        if dist <= elements.bond_cutoff(a.element, other.element, tol):
            n += 1
    return n


def _ref_overlaps(scene: Scene):
    out = []
    labels = list(scene.atoms)
    for ai in range(len(labels)):
        a = scene.atoms[labels[ai]]
        for bj in range(ai + 1, len(labels)):
            b = scene.atoms[labels[bj]]
            dist, _ = _ref_mic(scene.cell, a.frac, b.frac)
            floor = (
                elements.covalent_radius(a.element)
                + elements.covalent_radius(b.element)
            ) * validate.OVERLAP_FRACTION
            if dist < floor:
                out.append((a.label, b.label, round(dist, 3), round(floor, 3)))
    return out


def _ref_min_dist(scene: Scene) -> float:
    atoms = list(scene.atoms)
    if len(atoms) < 2:
        return 99.9
    best = 99.9
    for i in range(len(atoms)):
        for j in range(i + 1, len(atoms)):
            d, _ = _ref_mic(
                scene.cell, scene.atoms[atoms[i]].frac, scene.atoms[atoms[j]].frac
            )
            best = min(best, d)
    return best


def _ref_clean_sweep(scene: Scene) -> np.ndarray:
    cell = scene.cell
    labels = list(scene.atoms)
    disp = {label: np.zeros(3) for label in labels}
    for ai in range(len(labels)):
        a = scene.atoms[labels[ai]]
        for bj in range(ai + 1, len(labels)):
            b = scene.atoms[labels[bj]]
            d, img = _ref_mic(cell, a.frac, b.frac)
            target = elements.covalent_radius(a.element) + elements.covalent_radius(
                b.element
            )
            if 1e-6 < d < target * 0.98:
                vec = (b.frac + np.array(img) - a.frac) @ cell.lattice
                unit = vec / d
                push = (target - d) * 0.5
                disp[a.label] -= unit * push
                disp[b.label] += unit * push
    return np.array([disp[la] for la in labels]).reshape(-1, 3)


def _ref_relax_graph(
    elems,
    coords,
    bonds,
    pinned,
    *,
    hybridizations="sp3",
    iters=RELAX_ITERS,
    step=0.25,
    angle_step=0.02,
    angle_k=2.0,
    repulsion_margin=1.05,
    tol=None,
):
    n = len(elems)
    bonded = {frozenset(b) for b in bonds}
    movable = np.array([0.0 if i in pinned else 1.0 for i in range(n)])
    triples = angle_triples(elems, bonds, hybridizations)
    curve: list[float] = []
    converged = False
    step_count = 0
    cr = elements.covalent_radius
    for step_count in range(1, iters + 1):
        disp = np.zeros_like(coords)
        for i, j in bonds:
            target = cr(elems[i]) + cr(elems[j])
            d = coords[j] - coords[i]
            dist = float(np.linalg.norm(d))
            if dist < 1e-9:
                continue
            f = step * (dist - target) * (d / dist)
            disp[i] += f * movable[i]
            disp[j] -= f * movable[j]
        for i in range(n):
            for j in range(i + 1, n):
                if frozenset((i, j)) in bonded:
                    continue
                cutoff = 1.2 * (cr(elems[i]) + cr(elems[j])) * repulsion_margin
                d = coords[j] - coords[i]
                dist = float(np.linalg.norm(d))
                if dist >= cutoff or dist < 1e-9:
                    continue
                f = step * (cutoff - dist) * (d / dist)
                disp[i] -= f * movable[i]
                disp[j] += f * movable[j]
        for i, k, j, theta0 in triples:
            theta, gi, gj, gk = angle_theta_gradients(coords[i], coords[k], coords[j])
            f = angle_step * angle_k * (theta - theta0)
            disp[i] -= f * gi * movable[i]
            disp[j] -= f * gj * movable[j]
            disp[k] -= f * gk * movable[k]
        coords += disp
        max_disp = float(np.max(np.linalg.norm(disp, axis=1))) if n else 0.0
        curve.append(round(max_disp, 4))
        if tol is not None and max_disp < tol:
            converged = True
            break
    return GeoRelaxTrace(converged=converged, n_steps=step_count, curve=curve)


# -- fixtures ----------------------------------------------------------------

ORTHO = np.diag([9.0, 10.0, 11.0])
TRICLINIC = Cell.from_lengths_angles(8.0, 9.0, 10.0, 70.0, 80.0, 65.0).lattice
CELLS = {
    "ortho": Cell(ORTHO, (True, True, True)),
    "triclinic": Cell(TRICLINIC, (True, True, True)),
    "slab": Cell(ORTHO, (True, True, False)),
    "tube": Cell(TRICLINIC, (False, False, True)),
    "molecule": Cell(ORTHO, (False, False, False)),
}


def _scene(cell: Cell, n: int, seed: int, *, dense: bool = True) -> Scene:
    rng = np.random.default_rng(seed)
    sc = Scene(cell=cell)
    for k in range(n):
        el = ELS[int(rng.integers(len(ELS)))]
        f = rng.random(3)
        if dense and k % 7 == 0:
            f = f * 1.6 - 0.3  # some outside [0,1): exercises the base shift
        sc.atoms[f"a{el}{k + 1}"] = Atom(f"a{el}{k + 1}", el, f)
    return sc


@pytest.fixture(params=sorted(CELLS))
def cell(request) -> Cell:
    return CELLS[request.param]


# -- tests -------------------------------------------------------------------


def test_mic_matches_reference(cell: Cell) -> None:
    rng = np.random.default_rng(1)
    for _ in range(300):
        a = rng.random(3) * 3 - 1
        b = rng.random(3) * 3 - 1
        d, img = cell.mic(a, b)
        rd, rimg = _ref_mic(cell, a, b)
        assert abs(d - rd) <= TOL
        assert img == rimg


def test_mic_boundary_and_tie_cases() -> None:
    cell = Cell(ORTHO, (True, True, True))
    # exact half-cell separations: np.round half-to-even + first-wins tie-break
    pts = [0.0, 0.5, 1.0, 0.25, 0.75, -0.5, 1.5, 2.5]
    for x, y in itertools.product(pts, pts):
        a = np.array([x, 0.0, 0.5])
        b = np.array([y, 0.5, 0.0])
        d, img = cell.mic(a, b)
        rd, rimg = _ref_mic(cell, a, b)
        assert abs(d - rd) <= TOL
        assert img == rimg


@pytest.mark.parametrize("n", [0, 1, 2, 60, 200])
def test_detect_bonds_equivalent(cell: Cell, n: int) -> None:
    sc = _scene(cell, n, seed=10 + n)
    got = [(b.i, b.j, b.image) for b in probe.detect_bonds(sc)]
    assert got == _ref_detect(sc)
    assert all(b.provenance == "inferred" for b in probe.detect_bonds(sc))


def test_detect_bonds_nonempty_sanity() -> None:
    """The comparison isn't vacuous: dense random scenes do bond."""
    sc = _scene(CELLS["triclinic"], 120, seed=3)
    assert len(_ref_detect(sc)) > 10


@pytest.mark.parametrize("n", [1, 50, 150])
def test_coordination_equivalent(cell: Cell, n: int) -> None:
    sc = _scene(cell, n, seed=20 + n)
    allc = probe.coordination_all(sc)
    allcov = probe.covalent_coordination_all(sc)
    for la in list(sc.atoms)[:: max(1, n // 25)]:
        assert probe.coordination(sc, la) == _ref_coord(sc, la)
        assert probe.covalent_coordination(sc, la) == _ref_coord(sc, la, covalent=True)
    for la in sc.atoms:
        assert allc[la] == _ref_coord(sc, la)
        assert allcov[la] == _ref_coord(sc, la, covalent=True)


@pytest.mark.parametrize("n", [0, 1, 80, 200])
def test_validate_overlap_equivalent(cell: Cell, n: int) -> None:
    sc = _scene(cell, n, seed=30 + n)
    got = [
        (f.atoms[0], f.atoms[1], f.measured, f.expected)
        for f in validate.validate(sc)
        if f.rule == "atom_overlap"
    ]
    assert got == _ref_overlaps(sc)


@pytest.mark.parametrize("n", [0, 1, 2, 150])
def test_min_dist_equivalent(cell: Cell, n: int) -> None:
    sc = _scene(cell, n, seed=40 + n)
    assert abs(invariants._min_dist(sc) - _ref_min_dist(sc)) <= TOL


def test_neighbors_equivalent(cell: Cell) -> None:
    sc = _scene(cell, 80, seed=5)
    for la in list(sc.atoms)[:10]:
        got = sc.neighbors(la, 4.0)
        a = sc.atoms[la]
        ref = []
        for o in sc.atoms.values():
            d, img = _ref_mic(sc.cell, a.frac, o.frac)
            if o.label == la and d < 1e-9:
                continue
            if d <= 4.0:
                ref.append((o.label, img, d))
        ref.sort(key=lambda t: t[2])
        assert [(g[0], g[1]) for g in got] == [(r[0], r[1]) for r in ref]
        assert all(abs(g[2] - r[2]) <= TOL for g, r in zip(got, ref, strict=True))


def test_clean_sweep_and_relax_clean_equivalent(cell: Cell) -> None:
    from precis.structure import _pair_kernel

    sc = _scene(cell, 90, seed=6)
    labels, frac, lat, pbc = _pair_kernel.pack_scene(sc)
    radii = _pair_kernel.radii_of(sc, labels)
    got = _pair_kernel.clean_displacements(frac, lat, pbc, radii)
    assert np.abs(got - _ref_clean_sweep(sc)).max() <= TOL

    # whole _relax_clean vs a reference loop built from the reference sweep
    a = _scene(cell, 40, seed=7)
    b = _scene(cell, 40, seed=7)
    _relax_clean(a, steps=6, tol=1e-9)
    for _ in range(6):
        disp = _ref_clean_sweep(b)
        for k, atom in enumerate(b.atoms.values()):
            from precis.structure.relax import _free_axes

            dfrac = b.cell.cart_to_frac(disp[k]) * _free_axes(atom.fixed)
            atom.frac = b.cell.wrap(atom.frac + dfrac)
    for x, y in zip(a.atoms.values(), b.atoms.values(), strict=True):
        assert np.abs(x.frac - y.frac).max() <= 1e-8


def _graph(n: int, seed: int):
    rng = np.random.default_rng(seed)
    elems = [["C", "C", "C", "N", "O", "H"][int(rng.integers(6))] for _ in range(n)]
    coords = rng.random((n, 3)) * (n ** (1 / 3)) * 2.0
    bonds = [(i, i + 1) for i in range(n - 1)]
    bonds += [
        (int(a), int(b))
        for a, b in rng.integers(0, n, size=(n // 4, 2))
        if abs(int(a) - int(b)) > 1
    ]
    pinned = {0, 1, n - 1}
    return elems, coords, bonds, pinned


@pytest.mark.parametrize(
    ("n", "tol", "hyb"),
    [
        (0, None, "sp3"),
        (1, None, "sp3"),
        (2, 1e-3, "sp3"),
        (40, None, "sp2"),
        (100, 1e-4, "sp3"),
    ],
)
def test_relax_graph_equivalent(n: int, tol: float | None, hyb: str) -> None:
    elems, coords, bonds, pinned = (
        _graph(n, seed=n + 1) if n > 1 else (["C"] * n, np.zeros((n, 3)), [], set())
    )
    if n == 2:
        coords = np.array([[0.0, 0, 0], [3.0, 0, 0]])
        bonds = [(0, 1)]
    iters = 120 if n >= 40 else RELAX_ITERS
    c_new = coords.copy()
    c_ref = coords.copy()
    t_new = relax_graph(
        elems, c_new, bonds, pinned, hybridizations=hyb, iters=iters, tol=tol
    )
    t_ref = _ref_relax_graph(
        elems, c_ref, bonds, pinned, hybridizations=hyb, iters=iters, tol=tol
    )
    assert t_new.converged == t_ref.converged
    assert t_new.n_steps == t_ref.n_steps
    if n >= 2:
        assert np.abs(c_new - c_ref).max() <= 1e-8
        assert len(t_new.curve) == len(t_ref.curve)
        assert np.abs(np.array(t_new.curve) - np.array(t_ref.curve)).max() <= 1e-3
    if tol is not None and n == 2:
        assert t_new.converged


def test_relax_graph_noncontiguous_coords_written_back() -> None:
    elems, coords, bonds, pinned = _graph(30, seed=9)
    big = np.zeros((30, 6))
    big[:, :3] = coords
    view = big[:, :3]  # non-contiguous view
    ref = coords.copy()
    relax_graph(elems, view, bonds, pinned, iters=20)
    _ref_relax_graph(elems, ref, bonds, pinned, iters=20)
    assert np.abs(view - ref).max() <= 1e-8
    assert math.isfinite(float(view.sum()))


def test_relax_graph_coincident_angle_triple_matches_reference() -> None:
    elems = ["C", "C", "C", "C"]
    base = np.array([[0.0, 0, 0], [0.0, 0, 0], [1.5, 0, 0], [3.0, 1.0, 0]])
    bonds = [(0, 1), (1, 2), (2, 3)]  # atoms 0,1 coincide → d_ki == 0
    c_new, c_ref = base.copy(), base.copy()
    with np.errstate(all="ignore"):
        t_new = relax_graph(elems, c_new, bonds, set(), iters=5, tol=1e-3)
        t_ref = _ref_relax_graph(elems, c_ref, bonds, set(), iters=5, tol=1e-3)
    assert np.array_equal(np.isnan(c_new), np.isnan(c_ref))
    assert t_new.converged == t_ref.converged
    assert t_new.n_steps == t_ref.n_steps


def test_relax_graph_nan_never_converges() -> None:
    elems = ["C", "C", "C"]
    coords = np.array([[0.0, 0, 0], [1.5, 0, 0], [np.nan, 0, 0]])
    bonds = [(0, 1), (1, 2)]
    c_ref = coords.copy()
    with np.errstate(all="ignore"):
        t_new = relax_graph(elems, coords, bonds, set(), iters=4, tol=1e9)
        t_ref = _ref_relax_graph(elems, c_ref, bonds, set(), iters=4, tol=1e9)
    assert t_new.converged is False
    assert (t_new.converged, t_new.n_steps) == (t_ref.converged, t_ref.n_steps)


def test_unknown_label_raises_keyerror() -> None:
    sc = _scene(CELLS["ortho"], 5, seed=1)
    with pytest.raises(KeyError):
        sc.neighbors("aZz999", 3.0)
    with pytest.raises(KeyError):
        probe.coordination(sc, "aZz999")
    with pytest.raises(KeyError):
        probe.covalent_coordination(sc, "aZz999")


def test_relax_graph_readonly_coords_raises_like_before() -> None:
    elems, coords, bonds, pinned = _graph(10, seed=2)
    coords.setflags(write=False)
    with pytest.raises(ValueError, match="read-only"):
        relax_graph(elems, coords, bonds, pinned, iters=3)
