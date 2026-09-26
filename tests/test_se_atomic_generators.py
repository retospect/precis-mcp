"""Atomic-mode generators — the pure ``cnt``/``fullerene``/``cone``
builders (:mod:`precis_se.atomic.generators.sp2`) and the geometry their
declared envelopes promise.

nm's ``tests/test_nm_generators.py`` minus its handler half, ported by the
nm→se merge (docs/backlog/nm-se-merge.md): the ``generate`` op's
end-to-end/duplicate-name/orphan/slug-collision cases now live on
:class:`~precis_se.handler.SeHandler` in ``tests/test_se_atomic_bind.py``,
so what remains here is the store-free geometry — the half that never
needed a kind at all.

Geometry assertions are independent of the generators' own internal
bookkeeping wherever practical — bond lengths and the fullerene ring
census are recomputed straight from ``coords``/``bonds`` here, not read
back off ``topology``, so a bug that only shows up in the *realized atoms*
(not the declared topology dict) still fails a test.
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np
import pytest

from precis.cad import dsl as cad_dsl
from precis.structure.scene import Atom as StructAtom
from precis.structure.scene import Bond as StructBond
from precis.structure.scene import Scene as StructScene
from precis_se.atomic import validate as atomic_validate
from precis_se.atomic.generate import generated_cell, ingest_envelope
from precis_se.atomic.generators import GENERATORS, GeneratedBlock, GeneratorError
from precis_se.atomic.generators._types import ENVELOPE_UNIT, fmt_length_A
from precis_se.atomic.generators.sp2 import build_cnt, build_cone, build_fullerene
from precis_se.atomic.generators.sugars import build_cyclodextrin
from precis_se.atomic.generators.tpms import build_tpms
from precis_surface.dual import dualise
from precis_surface.level_set import schwarz_p, schwarz_p_grad
from precis_surface.periodic_mesh import periodic_mesh


def _spec_A(envelope: str) -> cad_dsl.ShapeSpec:
    """A generator's own Å figures, read back out of its **Å-suffixed**
    envelope text (nm-se-merge.md: generators write their unit into the
    DSL so the design side's ×1e-10 happens once, at the ``add_block``
    ingest boundary). Drop the unit and parse as bare numbers — the
    containment checks below live in the same Å space ``block.coords``
    does, not in stored metres."""
    return cad_dsl.parse(envelope.replace(ENVELOPE_UNIT, ""))


# ── shared geometry helpers (recompute from coords/bonds, no trust in the
#    generator's own declared topology) ────────────────────────────────


def _degrees(n_atoms: int, bonds: list[tuple[int, int, float, str]]) -> list[int]:
    deg = [0] * n_atoms
    for i, j, _order, _kind in bonds:
        deg[i] += 1
        deg[j] += 1
    return deg


def _bond_lengths(
    coords: np.ndarray, bonds: list[tuple[int, int, float, str]]
) -> list[float]:
    return [
        float(np.linalg.norm(coords[i] - coords[j])) for i, j, _order, _kind in bonds
    ]


def _ring_census(
    coords: np.ndarray, bonds: list[tuple[int, int, float, str]]
) -> Counter[int]:
    """Face sizes of the bond graph, via rotation-system face tracing.

    Every atom's neighbors are cyclically ordered by angle in the local
    tangent plane (using the atom's own position as the "up" direction —
    valid because a fullerene's atoms sit on a near-perfect shell), then
    each face is traced by always turning to the *next* neighbor in that
    order at each step (the standard combinatorial-map face-tracing
    algorithm) — a purely graph+geometry check, independent of the
    generator's own pentagon/hexagon bookkeeping.
    """
    n = len(coords)
    adj: dict[int, list[int]] = {i: [] for i in range(n)}
    for i, j, _order, _kind in bonds:
        adj[i].append(j)
        adj[j].append(i)

    def cyclic_order(v: int) -> list[int]:
        center = coords[v]
        normal = center / np.linalg.norm(center)
        arbitrary = np.array([1.0, 0.0, 0.0])
        if abs(np.dot(arbitrary, normal)) > 0.9:
            arbitrary = np.array([0.0, 1.0, 0.0])
        e1 = np.cross(normal, arbitrary)
        e1 = e1 / np.linalg.norm(e1)
        e2 = np.cross(normal, e1)
        angles = []
        for nb in adj[v]:
            rel = coords[nb] - center
            rel = rel - np.dot(rel, normal) * normal
            angles.append(np.arctan2(np.dot(rel, e2), np.dot(rel, e1)))
        return [nb for _a, nb in sorted(zip(angles, adj[v], strict=True))]

    rot = {v: cyclic_order(v) for v in range(n)}
    visited: set[tuple[int, int]] = set()
    faces: list[list[int]] = []
    for u in range(n):
        for v in adj[u]:
            if (u, v) in visited:
                continue
            face = []
            cu, cv = u, v
            while (cu, cv) not in visited:
                visited.add((cu, cv))
                face.append(cu)
                order = rot[cv]
                nxt = order[(order.index(cu) - 1) % len(order)]
                cu, cv = cv, nxt
            faces.append(face)
    return Counter(len(f) for f in faces)


def test_fmt_length_A_rounds_to_exactly_4_decimals() -> None:
    """0.1 pm precision (module docstring) — a 5th-decimal digit must be
    dropped, not carried into the rendered token."""
    assert fmt_length_A(1.234567) == "1.2346Å"


# ── cnt geometry ─────────────────────────────────────────────────────────


def test_cnt_10_10_radius_matches_closed_form() -> None:
    block = build_cnt({"n": 10, "m": 10, "length_A": 20.0})
    assert abs(block.topology["radius_A"] - 6.78) < 0.01
    assert block.topology["chiral_index"] == [10, 10]
    assert block.topology["pentagons"] == 0


def test_cnt_10_10_bond_lengths_in_range() -> None:
    block = build_cnt({"n": 10, "m": 10, "length_A": 20.0})
    lengths = _bond_lengths(block.coords, block.bonds)
    assert lengths  # sanity: some bonds exist
    assert all(1.38 <= x <= 1.46 for x in lengths)


def test_cnt_10_10_non_rim_atoms_are_3_coordinate() -> None:
    block = build_cnt({"n": 10, "m": 10, "length_A": 20.0})
    deg = _degrees(len(block.elements), block.bonds)
    rim_indices = {i for i, d in enumerate(deg) if d < 3}
    for i, d in enumerate(deg):
        if i not in rim_indices:
            assert d == 3, f"atom {i} has degree {d}, expected 3 (non-rim)"
    # every rim (open-valence) atom got a port, one-to-one
    assert {p.atom_index for p in block.ports} == rim_indices
    assert len(block.ports) == len(rim_indices)
    for p in block.ports:
        assert p.roles == ["covalent", "sp2-rim"]
        assert p.expected_element == "C"


def test_cnt_10_10_interior_atom_order_sums_to_4() -> None:
    """gripe 279306: Pauling order 4/3 on every bond, so a 3-coordinate
    interior atom's declared valence sums to exactly carbon's max valence
    of 4 (within float64 rounding — the float32-storage round-trip
    tolerance lives in ``structure/validate.py``, exercised at the
    handler/store level by ``test_generate_cnt_end_to_end``)."""
    block = build_cnt({"n": 10, "m": 10, "length_A": 20.0})
    deg = _degrees(len(block.elements), block.bonds)
    totals = [0.0] * len(block.elements)
    for i, j, order, kind in block.bonds:
        assert kind == "aromatic"
        totals[i] += order
        totals[j] += order
    interior = [i for i, d in enumerate(deg) if d == 3]
    assert interior  # sanity: some interior (non-rim) atoms exist
    for i in interior:
        assert abs(totals[i] - 4.0) < 1e-9


def test_cnt_10_10_atoms_inside_envelope() -> None:
    block = build_cnt({"n": 10, "m": 10, "length_A": 20.0})
    spec = _spec_A(block.envelope)
    assert spec.alias == "cyl"
    r, h = spec.params["r"], spec.params["h"]
    radial = np.linalg.norm(block.coords[:, :2], axis=1)
    assert np.all(radial <= r + 1e-6)
    assert np.all(block.coords[:, 2] >= -1e-6)
    assert np.all(block.coords[:, 2] <= h + 1e-6)


# ── cnt param rejection ────────────────────────────────────────────────


def test_cnt_m_greater_than_n_rejected() -> None:
    with pytest.raises(GeneratorError, match="0 <= m <= n"):
        build_cnt({"n": 5, "m": 8, "length_A": 10.0})


def test_cnt_n_zero_rejected() -> None:
    with pytest.raises(GeneratorError, match="positive integer"):
        build_cnt({"n": 0, "m": 0, "length_A": 10.0})


def test_cnt_absurd_length_rejected() -> None:
    with pytest.raises(GeneratorError, match="length_A"):
        build_cnt({"n": 10, "m": 10, "length_A": 100000.0})


# ── fullerene geometry ───────────────────────────────────────────────────


def test_fullerene_60_atom_and_bond_counts() -> None:
    block = build_fullerene({"atoms": 60})
    assert len(block.elements) == 60
    assert len(block.coords) == 60
    assert len(block.bonds) == 90
    assert block.ports == []
    assert block.topology == {"pentagons": 12, "hexagons": 20}


def test_fullerene_60_envelope_radius_is_shell_radius_plus_vdw_margin() -> None:
    """The declared sphere ADDS the vdW margin onto the realized shell
    radius (containment, module intent) — subtracting it would declare an
    envelope smaller than the atoms it must hold, failing
    ``envelope_fit`` outright."""
    from precis_se.atomic.generators.sp2 import VDW_MARGIN_A

    block = build_fullerene({"atoms": 60})
    shell_radius = float(np.max(np.linalg.norm(block.coords, axis=1)))
    spec = _spec_A(block.envelope)
    assert spec.alias == "sphere"
    assert spec.params["r"] == pytest.approx(shell_radius + VDW_MARGIN_A, abs=1e-3)


def test_fullerene_60_every_atom_3_coordinate() -> None:
    block = build_fullerene({"atoms": 60})
    deg = _degrees(60, block.bonds)
    assert deg == [3] * 60


def test_fullerene_60_ring_census_12_pentagons_20_hexagons() -> None:
    block = build_fullerene({"atoms": 60})
    sizes = _ring_census(block.coords, block.bonds)
    assert sizes[5] == 12
    assert sizes[6] == 20
    assert set(sizes) == {5, 6}


def test_fullerene_60_bond_lengths_cluster_at_experimental_values() -> None:
    block = build_fullerene({"atoms": 60})
    lengths = _bond_lengths(block.coords, block.bonds)
    short = [x for x in lengths if x < 1.43]
    long_ = [x for x in lengths if x >= 1.43]
    assert len(short) == 30  # 6:6 bonds
    assert len(long_) == 60  # 5:6 bonds
    assert all(abs(x - 1.401) < 0.01 for x in short)
    assert all(abs(x - 1.458) < 0.01 for x in long_)


def test_fullerene_60_kekule_bond_orders_honest() -> None:
    """gripe 279306: 30 double (6:6) + 60 single (5:6) bonds, and every
    atom has exactly one double-bond neighbor — the isolated-pentagon
    rule, checked straight off the returned bond quadruples (never re-
    trusting the generator's own internal Kekulé-assignment guard)."""
    block = build_fullerene({"atoms": 60})
    doubles = [(i, j) for i, j, order, _kind in block.bonds if order == 2.0]
    singles = [(i, j) for i, j, order, _kind in block.bonds if order == 1.0]
    assert len(doubles) == 30
    assert len(singles) == 60
    assert len(doubles) + len(singles) == len(block.bonds)
    double_count = [0] * 60
    for i, j, order, kind in block.bonds:
        if order == 2.0:
            double_count[i] += 1
            double_count[j] += 1
        assert kind == "pairwise"  # not "aromatic" — a definite Kekulé bond
    assert double_count == [1] * 60


def test_fullerene_70_unsupported() -> None:
    with pytest.raises(GeneratorError, match="60"):
        build_fullerene({"atoms": 70})


# ── cone geometry (round 2) ─────────────────────────────────────────────
#
# Rim detection below is independent of the generator's own under-
# coordination/port bookkeeping (reviewer finding on the round-2 diff: a
# test deriving "rim" from the generator's own degree<3 signal can never
# catch a seam bug that silently drops or mislabels atoms — the same
# self-referential gap that let gripe 279306 ship). Ground truth instead
# comes from each atom's own slant distance from the apex (``ρ = |coord|``
# — a fact about the realized 3D position, not the bond-detection output)
# compared against the cone's true boundaries, computed via
# :func:`_cone_rho_min` (the closed-form derivation, not any of
# ``build_cone``'s atom-retention/fold logic).


def _cone_rho_bounds(pentagons: int, length_A: float) -> tuple[float, float]:
    from precis_se.atomic.generators.sp2 import _cone_rho_min

    k = 6.0 / (6.0 - pentagons)
    rho_min = _cone_rho_min(k)
    return rho_min, rho_min + length_A


#: How close (as a fraction of the kept slant span) an under-coordinated
#: atom must sit to a true boundary to count as "a real rim, not a seam
#: bug" — generous against the observed true-rim spread at length_A=20
#: (small end: fractions up to ~0.04; large end: fractions down to ~0.93,
#: both driven by discrete ring spacing near the cutoffs), but still tight
#: enough to catch the reviewer's reproduced mid-cone artifact
#: (slant-fraction ~0.467).
_CONE_RIM_BAND_FRAC = 0.15


@pytest.mark.parametrize("pentagons", [1, 5])
def test_cone_bond_lengths_in_range(pentagons: int) -> None:
    block = build_cone({"pentagons": pentagons, "length_A": 20.0})
    lengths = _bond_lengths(block.coords, block.bonds)
    assert lengths  # sanity: some bonds exist
    assert all(1.38 <= x <= 1.47 for x in lengths)


@pytest.mark.parametrize("pentagons", [1, 2, 3, 4, 5])
def test_cone_under_coordinated_atoms_are_only_near_the_true_rims(
    pentagons: int,
) -> None:
    length_A = 20.0
    block = build_cone({"pentagons": pentagons, "length_A": length_A})
    deg = _degrees(len(block.elements), block.bonds)
    assert max(deg) == 3  # never over-coordinated
    rho_min, rho_max = _cone_rho_bounds(pentagons, length_A)
    span = rho_max - rho_min
    for i, d in enumerate(deg):
        if d >= 3:
            continue
        rho = float(np.linalg.norm(block.coords[i]))
        frac = (rho - rho_min) / span
        assert frac <= _CONE_RIM_BAND_FRAC or frac >= 1.0 - _CONE_RIM_BAND_FRAC, (
            f"P={pentagons}: atom {i} is under-coordinated (degree {d}) at "
            f"slant-fraction {frac:.3f} — neither true rim (mid-cone seam-"
            "bug signature)"
        )
    # every under-coordinated atom still got a declared port, one-to-one,
    # and both rims are represented (the generator-bookkeeping cross-check
    # this test complements, not replaces).
    rim_indices = {i for i, d in enumerate(deg) if d < 3}
    assert {p.atom_index for p in block.ports} == rim_indices
    assert len(block.ports) == len(rim_indices)
    assert any(p.name.startswith("rim_small") for p in block.ports)
    assert any(p.name.startswith("rim_large") for p in block.ports)
    for p in block.ports:
        assert p.roles == ["covalent", "sp2-rim"]
        assert p.expected_element == "C"


@pytest.mark.parametrize("pentagons", [1, 2, 3, 4, 5])
def test_cone_no_duplicate_atoms(pentagons: int) -> None:
    block = build_cone({"pentagons": pentagons, "length_A": 15.0})
    coords = block.coords
    n = len(coords)
    for i in range(n):
        for j in range(i + 1, n):
            assert float(np.linalg.norm(coords[i] - coords[j])) > 1.0


def _cone_omega_zero_reference_positions(
    pentagons: int, length_A: float, n_max: int = 8
) -> list[np.ndarray]:
    """Independent ground truth for the atoms that must survive on the
    omega=0 cut ray (reviewer finding: a site whose TRUE angle-from-apex
    is exactly 0 can land on float noise like ``rel[1] = -2.2e-16``,
    wrapping ``% (2*pi)`` to ``2*pi`` instead of ``~0`` and getting
    silently dropped by the retention filter). Derived by hand from the
    lattice geometry directly — not by calling :func:`build_cone` or
    reusing any of its retention/fold logic: the A-sublattice site at
    lattice coordinates ``(p1, p2) = (t+1, t)`` sits EXACTLY on the
    omega=0 ray for every integer ``t >= 0`` (worked out from
    ``apex = (2/3, -1/3)`` in the ``(a1, a2)`` basis: the Cartesian
    y-coordinate of ``site - apex`` is proportional to ``p1 - p2 - 1``,
    zero exactly when ``p1 = p2 + 1``, and its x-coordinate is positive
    for every ``t >= 0``, i.e. angle 0 rather than 180°)."""
    from precis_se.atomic.generators.sp2 import GRAPHENE_A, _cone_rho_min

    a1 = GRAPHENE_A * np.array([math.sqrt(3) / 2, 0.5])
    a2 = GRAPHENE_A * np.array([math.sqrt(3) / 2, -0.5])
    apex = (2.0 / 3.0) * a1 - (1.0 / 3.0) * a2
    k = 6.0 / (6.0 - pentagons)
    rho_min = _cone_rho_min(k)
    rho_max = rho_min + length_A
    half_angle = math.asin(1.0 - pentagons / 6.0)
    out = []
    for t in range(n_max):
        p1, p2 = t + 1, t
        rel = p1 * a1 + p2 * a2 - apex
        rho = float(np.linalg.norm(rel))
        if rho < rho_min or rho > rho_max:
            continue
        r_cyl = rho * math.sin(half_angle)
        z = rho * math.cos(half_angle)
        out.append(np.array([r_cyl, 0.0, z]))  # omega=0 -> cone_angle=0
    return out


@pytest.mark.parametrize("pentagons", [1, 2, 3, 4, 5])
def test_cone_omega_zero_ray_atoms_are_retained(pentagons: int) -> None:
    """Regression for the reviewer's seam-drop finding: every analytically-
    derived omega=0-ray atom in range must actually appear in
    ``build_cone``'s output (before the fix, these were silently dropped
    by the angular-window filter)."""
    length_A = 20.0
    block = build_cone({"pentagons": pentagons, "length_A": length_A})
    expected = _cone_omega_zero_reference_positions(pentagons, length_A)
    assert expected  # sanity: at least one ring's worth of omega=0 atoms in range
    for exp in expected:
        dists = np.linalg.norm(block.coords - exp, axis=1)
        assert dists.min() < 1e-6, (
            f"P={pentagons}: expected an atom at {exp} (analytic omega=0-"
            "ray ground truth) but none found in build_cone's output"
        )


@pytest.mark.parametrize("pentagons", [1, 5])
def test_cone_measured_apex_angle_matches_closed_form(pentagons: int) -> None:
    block = build_cone({"pentagons": pentagons, "length_A": 20.0})
    expected_deg = block.topology["cone_half_angle_deg"]
    for x, y, z in block.coords:
        measured_deg = math.degrees(math.atan2(math.hypot(x, y), z))
        assert abs(measured_deg - expected_deg) < 3.0


def test_cone_topology_facts() -> None:
    block = build_cone({"pentagons": 3, "length_A": 20.0})
    assert block.topology["pentagons"] == 3
    assert abs(block.topology["cone_half_angle_deg"] - 30.0) < 1e-6


def test_cone_envelope_is_a_tcone_primitive_containing_the_atoms() -> None:
    """gripe 286160: the envelope must be a ``tcone`` (apex at z=0, base at
    z=h — matching the atoms' own small-end-first z ordering), not the
    ``cone`` alias (whose apex sits at z=h, the *opposite* orientation —
    the original bug: a declared solid with its large end at z=0 tapering
    to a point at z=h, while the atoms run small-end-first)."""
    block = build_cone({"pentagons": 2, "length_A": 15.0})
    spec = _spec_A(block.envelope)
    assert spec.alias == "tcone"
    rb, rt, h = spec.params["rb"], spec.params["rt"], spec.params["h"]
    assert rb < rt  # small end (apex-ward, low z) narrower than the large end
    radial = np.linalg.norm(block.coords[:, :2], axis=1)
    z = block.coords[:, 2]
    assert np.all(z >= -1e-6)
    assert np.all(z <= h + 1e-6)
    # radius must bracket the atoms at their OWN height, not just overall —
    # this is what the original bug (declared apex at the wrong end) fails.
    envelope_radius_at_z = rb + (rt - rb) * z / h
    assert np.all(radial <= envelope_radius_at_z + 1e-6)


def _scene_from_block(block: GeneratedBlock) -> StructScene:
    """The same GeneratedBlock->Scene conversion
    :func:`precis_se.atomic.generate.prepare_generate` uses (its
    "generator geometry, exactly as realized" standard) — atoms
    unshifted into the block's own local frame, the exact frame
    ``envelope_fit`` compares the declared envelope against."""
    scene = StructScene(cell=generated_cell(block.coords))
    labels: list[str] = []
    for element, cart in zip(block.elements, block.coords, strict=True):
        label = scene.next_label(element)
        frac = scene.cell.wrap(scene.cell.cart_to_frac(np.asarray(cart, dtype=float)))
        scene.atoms[label] = StructAtom(
            label=label, element=element, frac=frac, hybridization=block.hybridization
        )
        labels.append(label)
    for i, j, order, kind in block.bonds:
        scene.bonds.append(StructBond(i=labels[i], j=labels[j], order=order, kind=kind))
    return scene


@pytest.mark.parametrize(
    "block",
    [
        build_cnt({"n": 6, "m": 0, "length_A": 15.0}),
        build_fullerene({"atoms": 60}),
        build_cone({"pentagons": 2, "length_A": 15.0}),
        # remesh=False: cheap fixture for an envelope-containment check,
        # unrelated to the {5,6,7} ring-purity ruling (module docstring).
        build_tpms({"family": "P", "cell_A": 8.0, "n": 11, "remesh": False}),
    ],
    ids=["cnt", "fullerene", "cone", "tpms"],
)
def test_generator_envelope_fit_reports_nothing(block: GeneratedBlock) -> None:
    """gripe 286160 regression: every convex-family generator's declared
    envelope must actually contain its own realized atoms (the L1<->L5
    agreement ``envelope_fit`` checks) — ``build_cone`` shipped a ``cone:``
    envelope with its apex/base swapped relative to where the atoms
    actually sit, a ~7.6 A worst-atom protrusion that this test would have
    caught immediately. Run over every closed-form sp² generator so the
    next one to get this wrong is caught here too, not by a live design's
    ``view='validate'`` warn-tier finding. (The cyclodextrin macrocycle is
    deliberately NOT in this list since gr332019 — its torus is
    bore-preserving, not fully containing; see the case below.)"""
    scene = _scene_from_block(block)
    # envelope_fit's `envelope` arg is STORED (design-space canonical)
    # text — bare metres — while a generator's own `block.envelope` is
    # Å-SUFFIXED raw output; round-trip through the same single ingest
    # boundary `generate` uses in production (`precis/utils/units.py`,
    # nm-se-merge.md: no handler-side pre-conversion any more).
    stored_env = ingest_envelope(block.envelope)
    assert atomic_validate.envelope_fit(stored_env, scene) is None


def test_tpms_atoms_inside_envelope() -> None:
    """The shared ``envelope_fit`` smoke test above only proves "no atom
    protrudes past the margin" -- an envelope declared absurdly oversized
    would pass that just as well, silently hiding a unit/frame mixup
    (Å atoms vs. a metres-scaled or otherwise mismeasured ``box:``). Check
    the actual box params directly (module docstring: centred x/y, base at
    z=0, precis.cad.primitives.box's own convention -- build_tpms shifts
    coords to match) AND that the atom cloud isn't a speck in a cavernous
    box, i.e. it actually exercises containment rather than vacuously
    passing. ``remesh=False``: this is an envelope-containment check,
    unrelated to the {5,6,7} ring-purity ruling (module docstring), so it
    keeps the cheap unremeshed fixture."""
    block = build_tpms({"family": "P", "cell_A": 8.0, "n": 11, "remesh": False})
    spec = _spec_A(block.envelope)
    assert spec.alias == "box"
    w, d, h = spec.params["w"], spec.params["d"], spec.params["h"]
    x, y, z = block.coords[:, 0], block.coords[:, 1], block.coords[:, 2]
    assert np.all(x >= -w / 2.0 - 1e-6)
    assert np.all(x <= w / 2.0 + 1e-6)
    assert np.all(y >= -d / 2.0 - 1e-6)
    assert np.all(y <= d / 2.0 + 1e-6)
    assert np.all(z >= -1e-6)
    assert np.all(z <= h + 1e-6)
    envelope_diag = math.sqrt(w * w + d * d + h * h)
    cloud_diag = float(
        np.linalg.norm(block.coords.max(axis=0) - block.coords.min(axis=0))
    )
    # "envelope big enough to contain anything" cannot pass this: the
    # scaffold's own atom cloud must span a real fraction of the declared
    # box, not sit as a speck inside a wildly oversized one.
    assert cloud_diag > 0.5 * envelope_diag


def test_cyclodextrin_envelope_fit_protrusion_is_bounded() -> None:
    """gr332019: the macrocycle's torus pins its BORE open (threading is
    the fact the envelope must not lie about), so rim atoms folded toward
    the axis may protrude and surface as ``envelope_fit``'s warn-tier
    finding — that is the design, not a defect. What must hold: the
    protrusion is a rim fold (small), never a cone-style gross mismatch."""
    block = build_cyclodextrin({"variant": "beta"})
    scene = _scene_from_block(block)
    stored_env = ingest_envelope(block.envelope)
    fit = atomic_validate.envelope_fit(stored_env, scene)
    if fit is not None:
        # A generator scene lives in the block's local frame by contract, so
        # the gr334764 frame-mismatch refusal must never fire here.
        assert not isinstance(fit, atomic_validate.FrameMismatch), fit
        _worst_atom, depth = fit
        assert depth < 2.0, f"gross envelope mismatch, not a rim fold: {fit}"


# ── cone param rejection ─────────────────────────────────────────────────


def test_cone_pentagons_zero_rejected() -> None:
    with pytest.raises(GeneratorError, match="flat"):
        build_cone({"pentagons": 0, "length_A": 10.0})


def test_cone_pentagons_six_rejected() -> None:
    with pytest.raises(GeneratorError, match="capped-tube"):
        build_cone({"pentagons": 6, "length_A": 10.0})


def test_cone_pentagons_out_of_range_rejected() -> None:
    with pytest.raises(GeneratorError, match="Euler counting"):
        build_cone({"pentagons": 11, "length_A": 10.0})


def test_cone_pentagons_negative_rejected() -> None:
    with pytest.raises(GeneratorError, match="Euler counting"):
        build_cone({"pentagons": -1, "length_A": 10.0})


def test_cone_length_absurd_rejected() -> None:
    with pytest.raises(GeneratorError, match="length_A"):
        build_cone({"pentagons": 3, "length_A": 100000.0})


def test_cone_missing_params_rejected() -> None:
    with pytest.raises(GeneratorError, match="pentagons"):
        build_cone({})


# ── registry ─────────────────────────────────────────────────────────────


def test_registry_has_round_1_and_round_2_generators() -> None:
    assert set(GENERATORS) == {
        "cnt",
        "fullerene",
        "cone",
        "cyclodextrin",
        "hexfold",
        "tpms",
        "schwarzite",
    }
    for builder in GENERATORS.values():
        assert callable(builder)


def test_generated_cell_size_is_double_the_extent_plus_fixed_margin() -> None:
    """A comfortably-containing, non-periodic cube (module docstring):
    ``size = 2*extent + 20`` — a plain doubling with no margin would clip
    atoms sitting near the extent, and a *subtracted* margin would produce
    a cell smaller than the atoms it must hold."""
    coords = np.array([[7.5, 0.0, 0.0], [-3.0, 2.0, 0.0]])
    cell = generated_cell(coords)
    assert cell.lattice[0, 0] == pytest.approx(2.0 * 7.5 + 20.0)
    assert cell.lattice[1, 1] == pytest.approx(2.0 * 7.5 + 20.0)
    assert cell.lattice[2, 2] == pytest.approx(2.0 * 7.5 + 20.0)


def test_generate_block_is_a_generated_block_type() -> None:
    # sanity: the dataclass shape `generate` relies on stays as documented
    block = build_fullerene({"atoms": 60})
    assert isinstance(block, GeneratedBlock)
    assert block.provenance


# ── tpms (docs/backlog/precis-surface-kernel.md "Slice 1 -- the dual
#    route") ───────────────────────────────────────────────────────────


def test_tpms_p_reachable_through_the_registry_like_cnt() -> None:
    assert GENERATORS["tpms"] is build_tpms
    assert GENERATORS["schwarzite"] is build_tpms
    block = GENERATORS["tpms"]({"family": "P", "cell_A": 8.0, "n": 17})
    assert isinstance(block, GeneratedBlock)


def test_tpms_p_all_carbon_and_chi_per_cell() -> None:
    block = build_tpms({"family": "P", "cell_A": 8.0, "n": 17, "reps": (1, 1, 1)})
    assert block.elements == ["C"] * len(block.elements)
    assert block.topology["chi_per_cell"] == -4
    assert block.topology["family"] == "P"


def test_tpms_p_bond_count_is_3_over_2_atoms_minus_rim_deficits() -> None:
    """The dual graph is exactly 3-regular per periodic cell (every
    triangle has 3 edges); `reps=(1,1,1)` drops every wrap-crossing bond
    (both cell axes' neighbour is out of the 1-cell box), so the realized
    bond count is `3/2 * atoms - (dropped wrap bonds)` -- checked against
    an independent recomputation of the base periodic net, not against the
    generator's own bookkeeping. `remesh=False`: this is a statement about
    the dual-graph construction itself (3-regularity holds on any
    triangulation, remeshed or not), so it is checked against the RAW
    periodic net below, not a remeshed one -- keeping this test's own
    from-scratch fixture and the generator's output in lock-step."""
    cell_A, n = 8.0, 17
    block = build_tpms(
        {"family": "P", "cell_A": cell_A, "n": n, "reps": (1, 1, 1), "remesh": False}
    )
    n_atoms = len(block.elements)

    pm = periodic_mesh(
        lambda pts: schwarz_p(pts, cell_A),
        a=cell_A,
        n=n,
        grad=lambda pts: schwarz_p_grad(pts, cell_A),
    )
    dnet = dualise(
        pm,
        f=lambda pts: schwarz_p(pts, cell_A),
        grad=lambda pts: schwarz_p_grad(pts, cell_A),
    )
    assert len(dnet.atoms) == n_atoms
    wrap_bonds = int(np.sum(np.any(dnet.shifts != 0, axis=1)))
    assert wrap_bonds > 0, "fixture has no wrap-crossing bonds to drop"
    expected_bonds = (3 * n_atoms) // 2 - wrap_bonds
    assert (3 * n_atoms) % 2 == 0
    assert len(block.bonds) == expected_bonds
    for _i, _j, order, kind in block.bonds:
        assert order == pytest.approx(4.0 / 3.0)
        assert kind == "aromatic"


def test_tpms_p_reps_2x1x1_doubles_atoms() -> None:
    block1 = build_tpms({"family": "P", "cell_A": 8.0, "n": 17, "reps": (1, 1, 1)})
    block2 = build_tpms({"family": "P", "cell_A": 8.0, "n": 17, "reps": (2, 1, 1)})
    assert len(block2.elements) == 2 * len(block1.elements)


def test_tpms_rim_ports_point_outward_and_flag_sp2_rim() -> None:
    block = build_tpms({"family": "P", "cell_A": 8.0, "n": 17, "reps": (1, 1, 1)})
    assert block.ports  # reps=(1,1,1) has open boundary on every axis
    for p in block.ports:
        assert p.roles == ["covalent", "sp2-rim"]
        assert p.expected_element == "C"
        norm = float(np.linalg.norm(p.direction))
        assert norm == pytest.approx(1.0, abs=1e-6)


def test_tpms_even_n_rejected() -> None:
    with pytest.raises(GeneratorError, match="odd"):
        build_tpms({"family": "P", "cell_A": 8.0, "n": 16})


def test_tpms_bad_family_rejected() -> None:
    with pytest.raises(GeneratorError, match="family"):
        build_tpms({"family": "X", "cell_A": 8.0})


def test_tpms_missing_cell_A_rejected() -> None:
    with pytest.raises(GeneratorError, match="cell_A"):
        build_tpms({"family": "P"})


def test_tpms_gyroid_family_builds_and_reports_chi() -> None:
    """`remesh=False`: gyroid's default (remeshed) path currently REFUSES
    on the {5,6,7} ruling (its known seam-freeze residual, covered by
    ``test_tpms_gyroid_default_remesh_refuses_pending_seam_fix`` below) --
    this test's own intent is chi_per_cell, which the remesh loop leaves
    invariant either way, so it stays on the raw scaffold rather than
    getting entangled with that unrelated refusal."""
    block = build_tpms(
        {"family": "G", "cell_A": 8.0, "n": 17, "reps": (1, 1, 1), "remesh": False}
    )
    assert block.topology["chi_per_cell"] == -8


def test_tpms_p_default_remesh_ring_histogram_is_within_567() -> None:
    """The default (``remesh`` param defaults to ``True``) path enforces
    the {5,6,7} ruling at the product boundary (tpms.py module docstring's
    "Ring purity is enforced" section) -- measured exact histogram for
    Schwarz P at cell_A=8.0, n=17, reps=(1,1,1)."""
    block = build_tpms({"family": "P", "cell_A": 8.0, "n": 17, "reps": (1, 1, 1)})
    rings = block.topology["rings"]
    assert set(rings) <= {5, 6, 7}
    assert rings == {5: 114, 6: 700, 7: 138}


def test_tpms_p_remesh_false_yields_the_raw_scaffold() -> None:
    """The ``remesh=False`` escape hatch (tpms.py module docstring) skips
    both the remesh loop and its {5,6,7} enforcement -- the raw
    marching-cubes dual keeps ring sizes the ruling forbids, on purpose,
    so a caller can inspect what the raw scaffold produced."""
    block = build_tpms(
        {"family": "P", "cell_A": 8.0, "n": 17, "reps": (1, 1, 1), "remesh": False}
    )
    rings = block.topology["rings"]
    outside = {k: v for k, v in rings.items() if k not in (5, 6, 7)}
    assert outside, (
        f"expected the raw scaffold to have rings outside {{5,6,7}}: {rings}"
    )
    assert rings == {4: 158, 5: 114, 6: 532, 7: 108, 8: 158, 9: 10}


def test_tpms_gyroid_default_remesh_refuses_pending_seam_fix() -> None:
    """KNOWN LIMITATION, not a regression: gyroid's frozen wrap seam
    (precis_surface.remesh's module docstring) leaves 20 (of 1365) welded
    vertices no admissible collapse/split/flip can reach, so its default
    (remeshed) path REFUSES rather than silently emit disallowed ring
    sizes -- the intended behaviour per the ruling (tpms.py module
    docstring's "Ring purity is enforced" section). DELETE OR FLIP this
    test once that seam-freeze residual is fixed and gyroid generates
    cleanly by default."""
    with pytest.raises(GeneratorError) as excinfo:
        build_tpms({"family": "G", "cell_A": 8.0, "n": 17, "reps": (1, 1, 1)})
    msg = str(excinfo.value)
    assert "4: 13" in msg
    assert "8: 5" in msg
    assert "9: 2" in msg
    assert "seam" in msg
