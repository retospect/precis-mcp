"""Unit tests for the fragment-building ops (nm-kind.md slice 2): the pure
``ring``/``attach``/``from_smiles`` ops (:mod:`precis.structure.ops`) and the
handler-level ``import_fragment`` expansion (:mod:`precis.handlers.structure`).

Kernel-level (``ring``/``attach``/``from_smiles``) fixtures are DB-free,
molecule-mode (non-periodic) Cartesian geometry — same style as
``test_structure_vsepr.py`` — except one dedicated ``attach`` test under a
periodic cell, exercising the MIC unwrap. ``import_fragment`` needs the
store, so its tests are handler-level (same fixture pattern as
``test_structure_handler.py``). Every ``from_smiles`` test that actually
embeds a molecule is gated ``pytest.importorskip("rdkit")`` — the ``[chem]``
extra may be absent from a given test environment — except the dedicated
no-rdkit path, which fakes the ImportError via ``sys.modules`` poisoning
(the ``mendeleev``/``test_estimate_plugin.py`` precedent) so it's meaningful
even when rdkit genuinely is installed.
"""

from __future__ import annotations

import itertools
import json
import sys

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import NotFound
from precis.handlers.structure import StructureHandler
from precis.structure import OpError, Scene, apply_ops, probe, validate, vsepr
from precis.structure.cell import Cell
from precis.structure.elements import covalent_radius

# -- shared geometry helpers ---------------------------------------------


def _molecule_cell(size: float = 20.0) -> Cell:
    return Cell(np.eye(3) * size, pbc=(False, False, False))


#: The ideal tetrahedral angle (deg) between any two of a tripod's three
#: bonds, and between each bond and the tripod's open (4th) valence axis.
_TETRA = np.radians(109.47)


def _tripod_offsets(axis: np.ndarray) -> list[np.ndarray]:
    """Three unit vectors at the tetrahedral angle from ``axis`` (120° apart
    azimuthally), such that the sum of the three equals exactly ``-axis`` —
    so a fragment built from them has its "open" 4th valence pointing along
    ``+axis`` (attach's own ``-normalize(sum)`` construction recovers it)."""
    axis = axis / np.linalg.norm(axis)
    seed = (
        np.array([1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    )
    u = seed - (seed @ axis) * axis
    u = u / np.linalg.norm(u)
    v = np.cross(axis, u)
    offsets = []
    for k in range(3):
        phi = 2.0 * np.pi * k / 3.0
        d = np.cos(_TETRA) * axis + np.sin(_TETRA) * (np.cos(phi) * u + np.sin(phi) * v)
        offsets.append(d)
    return offsets


def _mint_atom(scene: Scene, element: str, cart: np.ndarray) -> str:
    """Apply one ``add_atom`` and return the freshly minted label."""
    before = set(scene.atoms)
    apply_ops(scene, [{"op": "add_atom", "element": element, "cart": cart.tolist()}])
    (new_label,) = set(scene.atoms) - before
    return new_label


def _add_ch3(
    scene: Scene, center: np.ndarray, axis: np.ndarray, bond_length: float = 1.09
) -> tuple[str, list[str]]:
    """Mint a tripod "CH3"-like fragment: a C at ``center`` with 3 declared
    C-H bonds, its open (4th, un-substituted) valence pointing along
    ``axis``. Returns ``(c_label, [h_label, h_label, h_label])``."""
    c_label = _mint_atom(scene, "C", center)
    h_labels = []
    for offset in _tripod_offsets(np.asarray(axis, dtype=float)):
        h_label = _mint_atom(scene, "H", center + bond_length * offset)
        apply_ops(scene, [{"op": "add_bond", "i": c_label, "j": h_label, "order": 1}])
        h_labels.append(h_label)
    return c_label, h_labels


# -- ring -------------------------------------------------------------------


def test_ring_hexagon_aromatic_geometry() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(scene, [{"op": "ring", "element": "C", "n": 6, "aromatic": True}])
    labels = sorted(scene.atoms, key=lambda lb: int(lb[2:]))
    assert len(labels) == 6

    expected_l = 2.0 * covalent_radius("C") * 0.915
    for k in range(6):
        i, j = labels[k], labels[(k + 1) % 6]
        assert probe.distance(scene, i, j) == pytest.approx(expected_l, abs=1e-6)

    for k in range(6):
        a, b, c = labels[(k - 1) % 6], labels[k], labels[(k + 1) % 6]
        assert probe.angle(scene, a, b, c) == pytest.approx(120.0, abs=1e-6)

    assert len(scene.bonds) == 6
    for bond in scene.bonds:
        assert bond.order == 1.5
        assert bond.kind == "aromatic"
        assert bond.provenance == "declared"
    for atom in scene.atoms.values():
        assert atom.hybridization == "sp2"


def test_ring_explicit_center_and_normal_lands_in_plane() -> None:
    scene = Scene(cell=_molecule_cell())
    center = np.array([2.0, -1.0, 3.0])
    normal = np.array([1.0, 1.0, 1.0])
    apply_ops(
        scene,
        [
            {
                "op": "ring",
                "element": "C",
                "n": 6,
                "aromatic": True,
                "center": center.tolist(),
                "normal": normal.tolist(),
            }
        ],
    )
    n_hat = normal / np.linalg.norm(normal)
    for atom in scene.atoms.values():
        cart = scene.cell.frac_to_cart(atom.frac)
        signed = float((cart - center) @ n_hat)
        assert signed == pytest.approx(0.0, abs=1e-6)


def test_ring_rejects_out_of_range_n() -> None:
    scene = Scene(cell=_molecule_cell())
    with pytest.raises(OpError, match=r"\[3, 12\]"):
        apply_ops(scene, [{"op": "ring", "element": "C", "n": 2}])
    with pytest.raises(OpError, match=r"\[3, 12\]"):
        apply_ops(scene, [{"op": "ring", "element": "C", "n": 13}])


def test_ring_passes_vsepr_advisories_with_no_strain_or_twist() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(scene, [{"op": "ring", "element": "C", "n": 6, "aromatic": True}])
    findings = vsepr.advisories(scene)
    rules = {f.rule for f in findings}
    assert "angle_strain" not in rules
    assert "pi_twist" not in rules
    assert findings == []  # the template must coexist cleanly with the warn tier


# -- attach -------------------------------------------------------------


def test_attach_two_fragments_correct_distance_and_far_side_angle() -> None:
    scene = Scene(cell=_molecule_cell())
    c1, h1s = _add_ch3(scene, np.array([0.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]))
    c2, _h2s = _add_ch3(scene, np.array([10.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]))

    apply_ops(scene, [{"op": "attach", "from": c1, "to": c2, "order": 1}])

    expected_dist = 2.0 * covalent_radius("C")
    assert probe.distance(scene, c1, c2) == pytest.approx(expected_dist, abs=1e-6)
    new_bond = next(b for b in scene.bonds if {b.i, b.j} == {c1, c2})
    assert new_bond.order == 1.0
    assert new_bond.provenance == "declared"

    # from's other neighbours (its 3 H's) stay on the far side of the new bond
    for h in h1s:
        ang = probe.angle(scene, h, c1, c2)
        assert ang > 90.0
        assert ang == pytest.approx(109.47, abs=0.5)


def test_attach_moves_the_whole_fragment_rigidly() -> None:
    scene = Scene(cell=_molecule_cell())
    c1, h1s = _add_ch3(scene, np.array([0.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]))
    c2, _h2s = _add_ch3(scene, np.array([8.0, 3.0, -2.0]), np.array([0.0, 1.0, 0.0]))

    internal_pairs = [(c1, h) for h in h1s] + list(itertools.combinations(h1s, 2))
    before = {p: probe.distance(scene, *p) for p in internal_pairs}

    apply_ops(scene, [{"op": "attach", "from": c1, "to": c2}])

    for pair, dist in before.items():
        assert probe.distance(scene, *pair) == pytest.approx(dist, abs=1e-6)


def test_attach_same_fragment_is_rejected() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "cart": [1.09, 0.0, 0.0]},
            {"op": "add_bond", "i": "aC1", "j": "aH1", "order": 1},
        ],
    )
    with pytest.raises(OpError, match="two fragments"):
        apply_ops(scene, [{"op": "attach", "from": "aC1", "to": "aH1"}])


def test_attach_to_isolated_atom_without_direction_raises() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(scene, [{"op": "add_atom", "element": "Na", "cart": [30.0, 30.0, 30.0]}])
    c1, _h1s = _add_ch3(scene, np.array([0.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]))
    with pytest.raises(OpError, match="direction"):
        apply_ops(scene, [{"op": "attach", "from": c1, "to": "aNa1"}])


def test_attach_to_isolated_atom_explicit_direction_works() -> None:
    scene = Scene(cell=_molecule_cell())
    apply_ops(scene, [{"op": "add_atom", "element": "Na", "cart": [30.0, 30.0, 30.0]}])
    c1, _h1s = _add_ch3(scene, np.array([0.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]))

    apply_ops(
        scene,
        [{"op": "attach", "from": c1, "to": "aNa1", "direction": [0.0, 0.0, 1.0]}],
    )

    expected_dist = covalent_radius("C") + covalent_radius("Na")
    to_cart = scene.cell.frac_to_cart(scene.atoms["aNa1"].frac)
    from_cart = scene.cell.frac_to_cart(scene.atoms[c1].frac)
    assert np.allclose(from_cart - to_cart, [0.0, 0.0, expected_dist], atol=1e-6)


def test_attach_under_pbc_unwraps_the_straddling_fragment() -> None:
    # A cell small enough (5 Å) that a tripod anchored near the x=0 wall,
    # pointing -x, wraps some of its H's clear across to x~5 — attach must
    # unwrap the fragment before rotating/translating, and the resulting
    # geometry (measured via MIC) must be unchanged.
    cell = Cell(np.eye(3) * 5.0, pbc=(True, True, True))
    scene = Scene(cell=cell)
    c1, h1s = _add_ch3(scene, np.array([0.3, 2.5, 2.5]), np.array([1.0, 0.0, 0.0]))
    c2, _h2s = _add_ch3(scene, np.array([2.5, 2.5, 2.5]), np.array([1.0, 0.0, 0.0]))

    internal_pairs = [(c1, h) for h in h1s]
    before = {p: probe.distance(scene, *p) for p in internal_pairs}

    apply_ops(scene, [{"op": "attach", "from": c1, "to": c2}])

    expected_dist = 2.0 * covalent_radius("C")
    assert probe.distance(scene, c1, c2) == pytest.approx(expected_dist, abs=1e-6)
    for pair, dist in before.items():
        assert probe.distance(scene, *pair) == pytest.approx(dist, abs=1e-6)


def test_attach_under_pbc_declares_bond_with_mic_image() -> None:
    """Regression (reviewer finding, 2026-08-31): the new attach bond must
    carry its MIC image, not a blind (0,0,0) — image-trusting probes
    (bonds_through_plane) evaluate the bond at ``j.frac + image``, so the
    declared-image segment length must equal the attach distance even when
    the wrapped ``from`` lands across a cell wall from ``to``."""
    cell = Cell(np.eye(3) * 5.0, pbc=(True, True, True))
    scene = Scene(cell=cell)
    # `to` hugs the x=0 wall with its one neighbour at +x, so d_to points -x
    # and the attached fragment wraps to x~5 — a genuine wall-crossing bond.
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": [0.2, 2.5, 2.5]},
            {"op": "add_atom", "element": "C", "cart": [1.74, 2.5, 2.5]},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 1},
            {"op": "add_atom", "element": "C", "cart": [3.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "H", "cart": [4.59, 0.5, 0.5]},
            {"op": "add_bond", "i": "aC3", "j": "aH1", "order": 1},
        ],
    )
    apply_ops(scene, [{"op": "attach", "from": "aC3", "to": "aC1"}])
    bond = next(b for b in scene.bonds if {b.i, b.j} == {"aC3", "aC1"})
    pi = scene.cell.frac_to_cart(scene.atoms[bond.i].frac)
    pj = scene.cell.frac_to_cart(scene.atoms[bond.j].frac + np.array(bond.image))
    assert float(np.linalg.norm(pj - pi)) == pytest.approx(
        2.0 * covalent_radius("C"), abs=1e-6
    )


# -- from_smiles ---------------------------------------------------------


def test_from_smiles_benzene_geometry() -> None:
    pytest.importorskip("rdkit")
    scene = Scene(cell=_molecule_cell())
    apply_ops(scene, [{"op": "from_smiles", "smiles": "c1ccccc1"}])

    counts: dict[str, int] = {}
    for atom in scene.atoms.values():
        counts[atom.element] = counts.get(atom.element, 0) + 1
    assert counts == {"C": 6, "H": 6}

    aromatic_c = [
        lb
        for lb, atom in scene.atoms.items()
        if atom.element == "C" and atom.hybridization == "sp2"
    ]
    assert len(aromatic_c) == 6

    aromatic_bonds = [b for b in scene.bonds if b.kind == "aromatic"]
    assert len(aromatic_bonds) == 6
    for b in aromatic_bonds:
        assert b.order == 1.5
        assert probe.distance(scene, b.i, b.j) == pytest.approx(1.39, abs=0.05)

    # ETKDG benzene should be clean against our own warn tier.
    findings = vsepr.advisories(scene)
    rules = {f.rule for f in findings}
    assert "angle_strain" not in rules


def test_from_smiles_ethanol_all_single_bonds_and_oxygen_coordination() -> None:
    pytest.importorskip("rdkit")
    scene = Scene(cell=_molecule_cell())
    apply_ops(scene, [{"op": "from_smiles", "smiles": "CCO"}])

    assert len(scene.atoms) == 9  # C2H6O: 2 C, 1 O, 6 H
    assert all(b.kind == "pairwise" and b.order == 1.0 for b in scene.bonds)

    o_label = next(lb for lb, a in scene.atoms.items() if a.element == "O")
    o_neighbors = [b for b in scene.bonds if o_label in (b.i, b.j)]
    assert len(o_neighbors) == 2


def test_from_smiles_deterministic_per_seed() -> None:
    pytest.importorskip("rdkit")
    scene1 = Scene(cell=_molecule_cell())
    scene2 = Scene(cell=_molecule_cell())
    apply_ops(scene1, [{"op": "from_smiles", "smiles": "CCO", "seed": 7}])
    apply_ops(scene2, [{"op": "from_smiles", "smiles": "CCO", "seed": 7}])

    carts1 = sorted(
        tuple(round(x, 9) for x in scene1.cell.frac_to_cart(a.frac))
        for a in scene1.atoms.values()
    )
    carts2 = sorted(
        tuple(round(x, 9) for x in scene2.cell.frac_to_cart(a.frac))
        for a in scene2.atoms.values()
    )
    assert len(carts1) == len(carts2)
    for c1, c2 in zip(carts1, carts2):
        assert np.allclose(c1, c2, atol=1e-6)


def test_from_smiles_offset_honored() -> None:
    pytest.importorskip("rdkit")
    scene_a = Scene(cell=_molecule_cell())
    scene_b = Scene(cell=_molecule_cell())
    offset = np.array([5.0, -2.0, 1.0])
    apply_ops(scene_a, [{"op": "from_smiles", "smiles": "CCO", "seed": 3}])
    apply_ops(
        scene_b,
        [{"op": "from_smiles", "smiles": "CCO", "seed": 3, "offset": offset.tolist()}],
    )

    # same smiles/seed mints the same labels in the same order — compare
    # label-for-label rather than re-sorting (offset shifts x/y/z unevenly).
    assert set(scene_a.atoms) == set(scene_b.atoms)
    for label in scene_a.atoms:
        cart_a = scene_a.cell.frac_to_cart(scene_a.atoms[label].frac)
        cart_b = scene_b.cell.frac_to_cart(scene_b.atoms[label].frac)
        assert np.allclose(cart_b - cart_a, offset, atol=1e-6)


def test_from_smiles_bad_smiles_raises() -> None:
    pytest.importorskip("rdkit")
    scene = Scene(cell=_molecule_cell())
    with pytest.raises(OpError, match="not parseable as SMILES"):
        apply_ops(scene, [{"op": "from_smiles", "smiles": "not_a_smiles((("}])


def test_from_smiles_without_chem_extra_raises_clean_operror(monkeypatch) -> None:
    """Poison ``sys.modules['rdkit']`` (the ``mendeleev`` precedent in
    ``test_estimate_plugin.py``) so ``import rdkit`` raises ``ImportError``
    regardless of whether the real package is installed in this environment
    — the op must surface a clean, retryable ``OpError`` naming the
    ``[chem]`` extra, never a bare traceback."""
    monkeypatch.setitem(sys.modules, "rdkit", None)
    scene = Scene(cell=_molecule_cell())
    with pytest.raises(OpError, match=r"\[chem\]"):
        apply_ops(scene, [{"op": "from_smiles", "smiles": "c1ccccc1"}])


def test_from_smiles_composes_with_attach() -> None:
    """The two slice-2 features compose: mint benzene, free up one ring
    carbon's valence (vacancy its H), then rigidly attach a hand-built CH3
    tripod fragment onto it."""
    pytest.importorskip("rdkit")
    scene = Scene(cell=_molecule_cell())
    apply_ops(scene, [{"op": "from_smiles", "smiles": "c1ccccc1"}])

    c_label = next(lb for lb, a in scene.atoms.items() if a.element == "C")
    h_label = next(
        (b.j if b.i == c_label else b.i)
        for b in scene.bonds
        if c_label in (b.i, b.j)
        and scene.atoms[b.j if b.i == c_label else b.i].element == "H"
    )
    apply_ops(scene, [{"op": "vacancy", "atom": h_label}])

    ch3_c, _h_labels = _add_ch3(
        scene, np.array([20.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0])
    )
    apply_ops(scene, [{"op": "attach", "from": ch3_c, "to": c_label}])

    assert any({b.i, b.j} == {ch3_c, c_label} for b in scene.bonds)


def test_from_smiles_carries_declared_formal_charge() -> None:
    """gr285775: a charged SMILES (tetramethylammonium, a quaternary N+)
    carries rdkit's ``GetFormalCharge()`` into ``Atom.charge`` — and that
    declared charge is what makes the validator's charge-aware valence
    budget pass the N+ centre on its own terms, not by coincidence."""
    pytest.importorskip("rdkit")
    scene = Scene(cell=_molecule_cell())
    apply_ops(scene, [{"op": "from_smiles", "smiles": "C[N+](C)(C)C"}])

    n_label = next(lb for lb, a in scene.atoms.items() if a.element == "N")
    assert scene.atoms[n_label].charge == 1
    # every carbon in this SMILES is neutral
    assert all(a.charge == 0 for a in scene.atoms.values() if a.element == "C")
    assert not any(f.atoms == [n_label] for f in validate(scene))


# -- import_fragment (handler-level; needs the store) ------------------


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


def _mol_cell_payload(size: float = 20.0) -> dict:
    return {"a": size, "b": size, "c": size, "pbc": [False, False, False]}


def test_import_fragment_maps_labels_and_preserves_bonds(structure, store) -> None:
    structure.put(
        id="frag_src",
        text=json.dumps(
            {
                "cell": _mol_cell_payload(),
                "ops": [
                    {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
                    {"op": "add_atom", "element": "C", "cart": [1.33, 0.0, 0.0]},
                    {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 2},
                ],
            }
        ),
    )
    structure.put(
        id="target1",
        text=json.dumps(
            {
                "cell": _mol_cell_payload(),
                "ops": [{"op": "add_atom", "element": "C", "cart": [10.0, 10.0, 10.0]}],
            }
        ),
    )

    resp = structure.edit(
        id="target1", ops=[{"op": "import_fragment", "design": "frag_src"}]
    )
    assert "imported 2 atom(s) from frag_src" in resp.body
    assert "aC1→aC2" in resp.body
    assert "aC2→aC3" in resp.body

    ref = store.get_ref(kind="structure", id="target1")
    scene, _ = store.structure_load(ref.id)
    assert scene.composition() == {"C": 3}
    bond = next(b for b in scene.bonds if {b.i, b.j} == {"aC2", "aC3"})
    assert bond.order == 2.0
    assert bond.provenance == "declared"

    # follow-up attach by the mapped labels
    follow = structure.edit(
        id="target1",
        ops=[
            {"op": "attach", "from": "aC2", "to": "aC1", "direction": [1.0, 0.0, 0.0]}
        ],
    )
    assert "edited" in follow.body
    scene2, _ = store.structure_load(ref.id)
    assert any({b.i, b.j} == {"aC1", "aC2"} for b in scene2.bonds)


def test_import_fragment_unknown_design_is_not_found(structure) -> None:
    structure.put(
        id="target2", text=json.dumps({"cell": _mol_cell_payload(), "ops": []})
    )
    with pytest.raises(NotFound):
        structure.edit(
            id="target2", ops=[{"op": "import_fragment", "design": "no-such-design"}]
        )


def test_import_fragment_into_itself_duplicates(structure, store) -> None:
    structure.put(
        id="selfy",
        text=json.dumps(
            {
                "cell": _mol_cell_payload(),
                "ops": [{"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]}],
            }
        ),
    )
    structure.edit(id="selfy", ops=[{"op": "import_fragment", "design": "selfy"}])
    ref = store.get_ref(kind="structure", id="selfy")
    scene, _ = store.structure_load(ref.id)
    assert scene.composition() == {"C": 2}
