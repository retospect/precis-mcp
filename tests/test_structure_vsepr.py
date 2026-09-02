"""Unit tests for the advisory VSEPR/geometry warn tier (:mod:`vsepr`).

DB-free, molecule-mode (non-periodic) fixtures: every scene here is a `Cell`
with ``pbc=(False, False, False)``, atoms placed by exact Cartesian geometry
(``add_atom`` with ``cart=``), never a magic fractional coordinate.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis.structure import Scene, apply_ops, validate, vsepr
from precis.structure.cell import Cell
from precis.structure.scene import Bond


def _molecule_cell() -> Cell:
    return Cell(np.eye(3) * 20.0, pbc=(False, False, False))


def _cyclopropane() -> Scene:
    """Three carbons, equilateral triangle, all-single-bond ring — a legal
    but heavily angle-strained (60° vs. the 109.47° sp3 ideal) molecule."""
    scene = Scene(cell=_molecule_cell())
    side = 1.5
    c1 = np.array([0.0, 0.0, 0.0])
    c2 = np.array([side, 0.0, 0.0])
    c3 = np.array([side / 2.0, side * np.sin(np.radians(60.0)), 0.0])
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": c1.tolist()},
            {"op": "add_atom", "element": "C", "cart": c2.tolist()},
            {"op": "add_atom", "element": "C", "cart": c3.tolist()},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 1},
            {"op": "add_bond", "i": "aC2", "j": "aC3", "order": 1},
            {"op": "add_bond", "i": "aC1", "j": "aC3", "order": 1},
        ],
    )
    return scene


def _ethene(twisted: bool) -> Scene:
    """A C=C double bond with two H substituents per carbon, either
    coplanar (``twisted=False``) or with the second CH2 group rotated 90°
    about the C-C axis (``twisted=True``)."""
    scene = Scene(cell=_molecule_cell())
    c1 = np.array([0.0, 0.0, 0.0])
    c2 = np.array([1.33, 0.0, 0.0])
    h1a = np.array([-0.5, 0.87, 0.0])
    h1b = np.array([-0.5, -0.87, 0.0])
    if twisted:
        h2a = np.array([1.83, 0.0, 0.87])
        h2b = np.array([1.83, 0.0, -0.87])
    else:
        h2a = np.array([1.83, 0.87, 0.0])
        h2b = np.array([1.83, -0.87, 0.0])
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": c1.tolist()},
            {"op": "add_atom", "element": "C", "cart": c2.tolist()},
            {"op": "add_atom", "element": "H", "cart": h1a.tolist()},
            {"op": "add_atom", "element": "H", "cart": h1b.tolist()},
            {"op": "add_atom", "element": "H", "cart": h2a.tolist()},
            {"op": "add_atom", "element": "H", "cart": h2b.tolist()},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 2},
            {"op": "add_bond", "i": "aC1", "j": "aH1", "order": 1},
            {"op": "add_bond", "i": "aC1", "j": "aH2", "order": 1},
            {"op": "add_bond", "i": "aC2", "j": "aH3", "order": 1},
            {"op": "add_bond", "i": "aC2", "j": "aH4", "order": 1},
        ],
    )
    return scene


# -- infer_hybridization ------------------------------------------------------


def test_infer_hybridization_ethane_sp3() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "C", "cart": [1.54, 0.0, 0.0]},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 1},
        ],
    )
    assert vsepr.infer_hybridization(scene, "aC1") == "sp3"


def test_infer_hybridization_ethene_declared_double_bond_sp2() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "C", "cart": [1.33, 0.0, 0.0]},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 2},
        ],
    )
    assert vsepr.infer_hybridization(scene, "aC1") == "sp2"


def test_infer_hybridization_acetylene_triple_bond_sp() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "C", "cart": [1.20, 0.0, 0.0]},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 3},
        ],
    )
    assert vsepr.infer_hybridization(scene, "aC1") == "sp"


def test_infer_hybridization_aromatic_kind_sp2() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "C", "cart": [1.40, 0.0, 0.0]},
            {
                "op": "add_bond",
                "i": "aC1",
                "j": "aC2",
                "order": 1.5,
                "kind": "aromatic",
            },
        ],
    )
    assert vsepr.infer_hybridization(scene, "aC1") == "sp2"


def test_infer_hybridization_declared_override_wins() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [
            {
                "op": "add_atom",
                "element": "C",
                "cart": [0.0, 0.0, 0.0],
                "hybridization": "sp",
            },
            {"op": "add_atom", "element": "C", "cart": [1.54, 0.0, 0.0]},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 1},
        ],
    )
    # bonds-only inference would say sp3 (single bond only) — the declaration wins.
    assert vsepr.infer_hybridization(scene, "aC1") == "sp"


def test_infer_hybridization_metal_is_none() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "C", "cart": [1.8, 0.0, 0.0]},
            {"op": "add_bond", "i": "aPd1", "j": "aC1", "order": 1},
        ],
    )
    assert vsepr.infer_hybridization(scene, "aPd1") is None


def test_infer_hybridization_inferred_only_bonds_is_none() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "C", "cart": [1.54, 0.0, 0.0]},
        ],
    )
    scene.bonds.append(Bond(i="aC1", j="aC2", provenance="inferred"))
    assert vsepr.infer_hybridization(scene, "aC1") is None


# -- angle_strain --------------------------------------------------------------


def test_angle_strain_bent_water_warns() -> None:
    scene = Scene(cell=_molecule_cell())
    theta = np.radians(150.0)
    h1 = np.array([0.96, 0.0, 0.0])
    h2 = 0.96 * np.array([np.cos(theta), np.sin(theta), 0.0])
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "O", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "cart": h1.tolist()},
            {"op": "add_atom", "element": "H", "cart": h2.tolist()},
            {"op": "add_bond", "i": "aO1", "j": "aH1", "order": 1},
            {"op": "add_bond", "i": "aO1", "j": "aH2", "order": 1},
        ],
    )
    findings = [f for f in vsepr.advisories(scene) if f.rule == "angle_strain"]
    assert len(findings) == 1
    assert findings[0].expected == 104.5
    assert findings[0].measured == pytest.approx(150.0, abs=0.1)


def test_angle_strain_near_ideal_is_clean() -> None:
    scene = Scene(cell=_molecule_cell())
    theta = np.radians(104.5)  # the O sp3 (lone-pair-adjusted) ideal itself
    h1 = np.array([0.96, 0.0, 0.0])
    h2 = 0.96 * np.array([np.cos(theta), np.sin(theta), 0.0])
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "O", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "cart": h1.tolist()},
            {"op": "add_atom", "element": "H", "cart": h2.tolist()},
            {"op": "add_bond", "i": "aO1", "j": "aH1", "order": 1},
            {"op": "add_bond", "i": "aO1", "j": "aH2", "order": 1},
        ],
    )
    assert not any(f.rule == "angle_strain" for f in vsepr.advisories(scene))


def test_angle_strain_ignores_metal_neighbor() -> None:
    scene = Scene(cell=_molecule_cell())
    theta = np.radians(90.0)  # deliberately off the sp3 109.47° ideal
    h1 = np.array([1.09, 0.0, 0.0])
    h2 = 1.09 * np.array([np.cos(theta), np.sin(theta), 0.0])
    pd = np.array([0.0, 0.0, 1.8])
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "cart": h1.tolist()},
            {"op": "add_atom", "element": "H", "cart": h2.tolist()},
            {"op": "add_atom", "element": "Pd", "cart": pd.tolist()},
            {"op": "add_bond", "i": "aC1", "j": "aH1", "order": 1},
            {"op": "add_bond", "i": "aC1", "j": "aH2", "order": 1},
            {"op": "add_bond", "i": "aC1", "j": "aPd1", "order": 1},
        ],
    )
    findings = [f for f in vsepr.advisories(scene) if f.rule == "angle_strain"]
    # only the H-C-H pair fires; the metal neighbour never enters a pair.
    assert len(findings) == 1
    assert "aPd1" not in findings[0].atoms


def test_angle_strain_ignores_inferred_neighbor() -> None:
    """Regression (reviewer finding, 2026-08-31): hybridization is inferred
    from declared bonds only, so the angle pairs must be too. One declared
    single bond defaults aC1 to sp3; an auto-detected (inferred) O neighbour
    at 180° must NOT be measured against the 109.47° sp3 ideal."""
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "C", "cart": [1.54, 0.0, 0.0]},
            {"op": "add_atom", "element": "O", "cart": [-1.20, 0.0, 0.0]},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 1},
        ],
    )
    scene.bonds.append(Bond(i="aC1", j="aO1", provenance="inferred"))
    assert not any(f.rule == "angle_strain" for f in vsepr.advisories(scene))


def test_angle_strain_ring_exemption_cyclopropane() -> None:
    findings = vsepr.advisories(_cyclopropane())
    assert not any(f.rule == "angle_strain" for f in findings)
    assert any(f.rule == "small_ring" for f in findings)


# -- pi_twist -------------------------------------------------------------------


def test_pi_twist_planar_ethene_is_clean() -> None:
    scene = _ethene(twisted=False)
    assert not any(f.rule == "pi_twist" for f in vsepr.advisories(scene))


def test_pi_twist_twisted_ethene_warns() -> None:
    scene = _ethene(twisted=True)
    findings = [f for f in vsepr.advisories(scene) if f.rule == "pi_twist"]
    assert len(findings) == 1
    assert findings[0].measured == pytest.approx(90.0, abs=5.0)
    assert findings[0].expected == vsepr.TWIST_TOL


def test_pi_twist_partial_double_pairwise_bond_checked() -> None:
    """An order-1.5 bond declared WITHOUT kind='aromatic' is still a π system
    (the sp2 inference already says so) — a twisted one must warn."""
    scene = _ethene(twisted=True)
    for b in scene.bonds:
        if {b.i, b.j} == {"aC1", "aC2"}:
            b.order = 1.5
            assert b.kind == "pairwise"
    findings = [f for f in vsepr.advisories(scene) if f.rule == "pi_twist"]
    assert len(findings) == 1
    assert findings[0].measured == pytest.approx(90.0, abs=5.0)


# -- small_ring -----------------------------------------------------------------


def test_small_ring_metal_triangle_is_clean() -> None:
    """A close-packed metal lattice is full of 3-atom triangles over
    auto-detected metal-metal bonds — lattice geometry, never a strained-ring
    finding."""
    scene = Scene(cell=_molecule_cell())
    side = 2.75  # ~Pd nearest-neighbour distance
    p1 = np.array([0.0, 0.0, 0.0])
    p2 = np.array([side, 0.0, 0.0])
    p3 = np.array([side / 2.0, side * np.sin(np.radians(60.0)), 0.0])
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "cart": p1.tolist()},
            {"op": "add_atom", "element": "Pd", "cart": p2.tolist()},
            {"op": "add_atom", "element": "Pd", "cart": p3.tolist()},
        ],
    )
    for i, j in (("aPd1", "aPd2"), ("aPd2", "aPd3"), ("aPd1", "aPd3")):
        scene.bonds.append(Bond(i=i, j=j, provenance="inferred"))
    assert not any(f.rule == "small_ring" for f in vsepr.advisories(scene))


def test_small_ring_cyclopropane_warns() -> None:
    findings = [f for f in vsepr.advisories(_cyclopropane()) if f.rule == "small_ring"]
    assert len(findings) == 1
    assert findings[0].measured == 3.0
    assert findings[0].expected == 5.0


def test_small_ring_six_ring_is_clean() -> None:
    scene = Scene(cell=_molecule_cell())
    n = 6
    radius = 1.4
    atom_ops: list[dict[str, object]] = []
    for k in range(n):
        ang = np.radians(60.0 * k)
        pos = radius * np.array([np.cos(ang), np.sin(ang), 0.0])
        atom_ops.append({"op": "add_atom", "element": "C", "cart": pos.tolist()})
    apply_ops(scene, atom_ops)
    bond_ops = [
        {"op": "add_bond", "i": f"aC{k + 1}", "j": f"aC{(k + 1) % n + 1}", "order": 1}
        for k in range(n)
    ]
    apply_ops(scene, bond_ops)
    assert not any(f.rule == "small_ring" for f in vsepr.advisories(scene))


# -- hybridization_conflict ------------------------------------------------------


def test_hybridization_conflict_declared_sp3_with_double_bond_warns() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [
            {
                "op": "add_atom",
                "element": "C",
                "cart": [0.0, 0.0, 0.0],
                "hybridization": "sp3",
            },
            {"op": "add_atom", "element": "C", "cart": [1.33, 0.0, 0.0]},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 2},
        ],
    )
    findings = [
        f for f in vsepr.advisories(scene) if f.rule == "hybridization_conflict"
    ]
    assert len(findings) == 1
    assert findings[0].atoms == ["aC1"]
    assert findings[0].expected == pytest.approx(109.47)  # declared sp3
    assert findings[0].measured == pytest.approx(120.0)  # bonds imply sp2


# -- metal_coordination (gr285775) ------------------------------------------------


def _zr_with_n_oxygens(n: int) -> Scene:
    """One Zr atom declared-bonded to ``n`` O atoms, spread out enough to
    dodge the unrelated atom_overlap/bond_too_long error-tier rules — the
    point here is exercising the metal-coordination COUNT, not geometry."""
    scene = Scene(cell=_molecule_cell())
    apply_ops(scene, [{"op": "add_atom", "element": "Zr", "cart": [0.0, 0.0, 0.0]}])
    ops: list[dict[str, object]] = []
    for k in range(n):
        # points on a sphere (golden-angle spiral) so no two O atoms collide
        phi = np.arccos(1 - 2 * (k + 0.5) / n)
        theta = np.pi * (1 + 5**0.5) * k
        d = (
            np.array(
                [
                    np.sin(phi) * np.cos(theta),
                    np.sin(phi) * np.sin(theta),
                    np.cos(phi),
                ]
            )
            * 2.2
        )  # ~Zr-O bond length
        ops.append({"op": "add_atom", "element": "O", "cart": d.tolist()})
        ops.append({"op": "add_bond", "i": "aZr1", "j": f"aO{k + 1}", "order": 1})
    apply_ops(scene, ops)
    return scene


def test_metal_coordination_normal_sbu_zr_is_clean() -> None:
    # Zr6O4(OH)4 (UiO-66 SBU)-style: 7 oxygens on one Zr, within the 6-8
    # advisory range.
    scene = _zr_with_n_oxygens(7)
    assert not any(f.rule == "metal_coordination" for f in vsepr.advisories(scene))


def test_metal_coordination_overcoordinated_zr_warns() -> None:
    scene = _zr_with_n_oxygens(20)
    findings = [f for f in vsepr.advisories(scene) if f.rule == "metal_coordination"]
    assert len(findings) == 1
    assert findings[0].atoms == ["aZr1"]
    assert findings[0].measured == 20.0
    assert findings[0].severity == "warn"


def test_metal_coordination_never_gates() -> None:
    scene = _zr_with_n_oxygens(20)
    assert validate(scene) == []  # advisory only — the hard-reject gate is silent


def test_metal_coordination_ignores_a_bare_slab_atom_with_no_declared_bonds() -> None:
    # A `slab`-built (or otherwise undecorated) metal atom declares zero
    # bonds at all — not "coordination chemistry" in this rule's sense, so
    # it must be skipped rather than misread as CN=0-out-of-range.
    scene = Scene(cell=_molecule_cell())
    apply_ops(scene, [{"op": "add_atom", "element": "Zn", "cart": [0.0, 0.0, 0.0]}])
    assert not any(f.rule == "metal_coordination" for f in vsepr.advisories(scene))


def test_metal_coordination_unmodeled_metal_is_silently_skipped() -> None:
    # Pd/Ni/Pt/Au are deliberately absent from the small table (they're this
    # codebase's slab metals; their correct bulk CN would false-flag) — no
    # finding, not a guessed range.
    scene = Scene(cell=_molecule_cell())
    apply_ops(scene, [{"op": "add_atom", "element": "Pd", "cart": [0.0, 0.0, 0.0]}])
    apply_ops(scene, [{"op": "add_atom", "element": "O", "cart": [2.0, 0.0, 0.0]}])
    apply_ops(scene, [{"op": "add_bond", "i": "aPd1", "j": "aO1", "order": 1}])
    assert not any(f.rule == "metal_coordination" for f in vsepr.advisories(scene))


# -- unmodeled_charge_state (gr285775) ----------------------------------------------


def test_unmodeled_charge_state_warns_for_an_untabulated_state() -> None:
    # N carrying charge +2 has no elements._CHARGED_VALENCE entry.
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [{"op": "add_atom", "element": "N", "cart": [0.0, 0.0, 0.0], "charge": 2}],
    )
    findings = [
        f for f in vsepr.advisories(scene) if f.rule == "unmodeled_charge_state"
    ]
    assert len(findings) == 1
    assert findings[0].atoms == ["aN1"]
    assert findings[0].severity == "warn"


def test_unmodeled_charge_state_silent_for_a_tabulated_state() -> None:
    # N+ (charge=+1) IS in the table — no advisory note.
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [{"op": "add_atom", "element": "N", "cart": [0.0, 0.0, 0.0], "charge": 1}],
    )
    assert not any(f.rule == "unmodeled_charge_state" for f in vsepr.advisories(scene))


def test_unmodeled_charge_state_silent_for_neutral_atoms() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(scene, [{"op": "add_atom", "element": "N", "cart": [0.0, 0.0, 0.0]}])
    assert not any(f.rule == "unmodeled_charge_state" for f in vsepr.advisories(scene))


# -- gate isolation ---------------------------------------------------------------


def test_validate_gate_never_sees_advisory_findings() -> None:
    scene = _cyclopropane()
    # strained (60° ring angles, 3-membered ring) but not physically
    # impossible — the hard-reject gate must stay silent.
    assert validate(scene) == []
    findings = vsepr.advisories(scene)
    assert findings
    assert all(f.severity == "warn" for f in findings)
