"""``precis_geom.mol`` slice 1 — fragments, attach, library
(molecular-workspace-code spec, slice 1 acceptance).

DB-free: RDKit + the geometry core only (the precis-geom wheel's graph).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rdkit")

from rdkit import Chem

import precis_geom.mol as mol
from precis.errors import BadInput
from precis.structure import probe
from precis.structure.cell import Cell
from precis.structure.ops import apply_ops
from precis.structure.scene import Scene
from precis_geom import trace
from precis_geom.assembly import Geom


def _cart(scene: Scene, label: str) -> np.ndarray:
    return np.asarray(scene.cell.frac_to_cart(scene.atoms[label].frac), dtype=float)


def _bond_vecs(scene: Scene, label: str) -> list[np.ndarray]:
    out = []
    for b in scene.bonds:
        if label in (b.i, b.j):
            other = b.j if b.i == label else b.i
            v = _cart(scene, other) - _cart(scene, label)
            out.append(v / np.linalg.norm(v))
    return out


def _canonical(scene: Scene) -> str:
    """RDKit canonical SMILES of a Scene's declared graph (explicit H,
    aromatic orders 1.5)."""
    rw = Chem.RWMol()
    idx: dict[str, int] = {}
    for la, a in scene.atoms.items():
        at = Chem.Atom(a.element)
        at.SetFormalCharge(a.charge)
        at.SetNoImplicit(True)
        idx[la] = rw.AddAtom(at)
    types = {
        1.0: Chem.BondType.SINGLE,
        1.5: Chem.BondType.AROMATIC,
        2.0: Chem.BondType.DOUBLE,
        3.0: Chem.BondType.TRIPLE,
    }
    for b in scene.bonds:
        rw.AddBond(idx[b.i], idx[b.j], types[float(b.order)])
        if b.order == 1.5:
            rw.GetAtomWithIdx(idx[b.i]).SetIsAromatic(True)
            rw.GetAtomWithIdx(idx[b.j]).SetIsAromatic(True)
    m = rw.GetMol()
    Chem.SanitizeMol(m)
    return Chem.MolToSmiles(Chem.RemoveHs(m))


def _ref(smiles: str) -> str:
    return Chem.MolToSmiles(Chem.MolFromSmiles(smiles))


def _ring_c(g: Geom) -> str:
    return next(la for la, a in g.scene.atoms.items() if a.element == "C")


# -- frag(smiles=) ----------------------------------------------------------


def test_frag_cooh_has_one_port_with_the_bond_vector() -> None:
    g = mol.frag(smiles="[*:1]C(=O)O")
    assert sorted(g.ports) == ["a1"]
    p = g.ports["a1"]
    s = g.scene
    # dummy removed: C, 2 O, the acid H
    assert s.composition() == {"C": 1, "O": 2, "H": 1}
    assert s.atoms[p.atom].element == "C"
    assert p.annotations == {"order": 1.0}
    d = np.asarray(p.direction)
    assert abs(np.linalg.norm(d) - 1.0) < 1e-9
    assert np.allclose(p.pos, _cart(s, p.atom), atol=1e-9)
    # the carboxyl C is trigonal: the open bond sits ~120 deg from both
    # of its other bonds and in their plane.
    v1, v2 = _bond_vecs(s, p.atom)
    for v in (v1, v2):
        assert math.degrees(math.acos(float(d @ v))) == pytest.approx(120.0, abs=12.0)
    assert abs(float(d @ np.cross(v1, v2))) < 0.2


def test_frag_two_ports_numbered_by_map_number() -> None:
    g = mol.frag(smiles="[*:2]CC[*:1]")
    assert sorted(g.ports) == ["a1", "a2"]
    assert g.ports["a1"].atom != g.ports["a2"].atom
    assert g.scene.composition() == {"C": 2, "H": 4}


def test_frag_deterministic_per_seed() -> None:
    a = mol.frag(smiles="[*:1]CCO", conf_seed=3)
    b = mol.frag(smiles="[*:1]CCO", conf_seed=3)
    for la in a.scene.atoms:
        assert np.allclose(_cart(a.scene, la), _cart(b.scene, la), atol=1e-12)


def test_frag_bad_smiles_and_arity() -> None:
    with pytest.raises(BadInput, match="not parseable"):
        mol.frag(smiles="not_a_smiles(((")
    with pytest.raises(BadInput, match="exactly one"):
        mol.frag()
    with pytest.raises(BadInput, match="used twice"):
        mol.frag(smiles="[*:1]C[*:1]")


def test_frag_ref_needs_the_read_mcp() -> None:
    with pytest.raises(BadInput, match="read MCP") as ei:
        mol.frag(ref="structure:benzene")
    assert "blocks.get" in str(ei.value)
    assert ei.value.next


def test_frag_from_scene_infers_port_from_free_valence() -> None:
    sc = Scene(cell=Cell(np.diag([30.0, 30.0, 30.0]), (False, False, False)))
    apply_ops(sc, [{"op": "from_smiles", "smiles": "O", "offset": [10, 10, 10]}])
    o = next(la for la, a in sc.atoms.items() if a.element == "O")
    g = mol.frag(scene=sc, ports={"a1": {"atom": o}})
    p = g.ports["a1"]
    assert np.allclose(p.pos, _cart(sc, o))
    # opposite the two O-H bonds' sum: away from the hydrogens
    s = -np.sum(_bond_vecs(sc, o), axis=0)
    assert float(np.asarray(p.direction) @ (s / np.linalg.norm(s))) > 0.5
    assert g.scene is not sc  # a copy; the input is untouched
    with pytest.raises(BadInput, match="no atom"):
        mol.frag(scene=sc, ports={"a1": {"atom": "aZz9"}})


# -- attach -------------------------------------------------------------------


def _benzoic(torsion: float = 0.0) -> tuple[Geom, Geom]:
    host = mol.frag(smiles="c1ccccc1")
    return host, mol.attach(
        host, _ring_c(host), mol.lib("COOH").port("a1"), torsion=torsion
    )


def test_attach_cooh_to_benzene_is_benzoic_acid() -> None:
    host, out = _benzoic()
    s = out.scene
    assert _canonical(s) == _ref("OC(=O)c1ccccc1")
    assert s.composition() == {"C": 7, "H": 6, "O": 2}
    # one scene, one H gone from the ring carbon, the new bond ~ C-C
    c = _ring_c(host)
    assert not any(
        s.atoms[o].element == "H"
        for b in s.bonds
        if c in (b.i, b.j)
        for o in ((b.j if b.i == c else b.i),)
    )
    other = next(
        (b.j if b.i == c else b.i)
        for b in s.bonds
        if c in (b.i, b.j) and (b.j if b.i == c else b.i) not in host.scene.atoms
    )
    length = float(np.linalg.norm(_cart(s, c) - _cart(s, other)))
    assert length == pytest.approx(1.52, abs=1e-6)  # 0.76 + 0.76
    assert out.findings == ()  # no attach.clash
    # the removed H's direction is the bond direction
    h_dirs = [
        v
        for b in host.scene.bonds
        if c in (b.i, b.j)
        for o in ((b.j if b.i == c else b.i),)
        if host.scene.atoms[o].element == "H"
        for v in [_cart(host.scene, o) - _cart(host.scene, c)]
    ]
    got = _cart(s, other) - _cart(s, c)
    assert float(got @ (h_dirs[0] / np.linalg.norm(h_dirs[0]))) == pytest.approx(
        length, abs=1e-6
    )


def test_attach_torsion_moves_only_fragment_atoms() -> None:
    host, out0 = _benzoic(0.0)
    _, out90 = _benzoic(90.0)
    s0, s90 = out0.scene, out90.scene
    host_labels = [la for la in s0.atoms if la in host.scene.atoms and la in s90.atoms]
    assert len(host_labels) >= 11  # the ring + 5 H (one H became the bond)
    for la in host_labels:
        assert np.allclose(_cart(s0, la), _cart(s90, la), atol=1e-9), la
    frag_labels = [la for la in s0.atoms if la not in host.scene.atoms]
    assert len(frag_labels) == 4
    c_ring = _ring_c(host)
    attach_c = next(la for la in frag_labels if s0.atoms[la].element == "C")
    # the attachment atom is on the rotation axis; the rest swing about it
    assert np.allclose(_cart(s0, attach_c), _cart(s90, attach_c), atol=1e-9)
    moved = [
        la
        for la in frag_labels
        if la != attach_c and not np.allclose(_cart(s0, la), _cart(s90, la), atol=1e-3)
    ]
    assert len(moved) == 3
    # and by exactly 90 degrees about the new bond
    ring_nb = next(
        (b.j if b.i == c_ring else b.i)
        for b in s0.bonds
        if c_ring in (b.i, b.j)
        and s0.atoms[(b.j if b.i == c_ring else b.i)].element == "C"
        and (b.j if b.i == c_ring else b.i) in host.scene.atoms
    )
    o_label = next(la for la in frag_labels if s0.atoms[la].element == "O")
    d0 = probe.dihedral(s0, ring_nb, c_ring, attach_c, o_label)
    d90 = probe.dihedral(s90, ring_nb, c_ring, attach_c, o_label)
    assert abs(((d90 - d0 + 180.0) % 360.0) - 180.0) == pytest.approx(90.0, abs=0.01)


def test_attach_to_port_site_and_carry_unused_ports() -> None:
    spacer = mol.lib("C2H4")
    one = mol.attach(spacer, spacer.port("a1"), mol.lib("OH").port("a1"))
    assert sorted(one.ports) == ["a2"]  # a1 consumed, a2 carried
    two = mol.attach(one, one.port("a2"), mol.lib("OH").port("a1"))
    assert two.ports == {}
    assert _canonical(two.scene) == _ref("OCCO")
    # the carried port still points out of its (moved) atom
    p = one.ports["a2"]
    assert one.scene.atoms[p.atom].element == "C"
    assert np.allclose(p.pos, _cart(one.scene, p.atom), atol=1e-9)


def test_attach_refuses_a_saturated_atom() -> None:
    host = mol.frag(smiles="c1ccccc1")
    h = next(la for la, a in host.scene.atoms.items() if a.element == "H")
    with pytest.raises(BadInput, match="open valence"):
        mol.attach(host, h, mol.lib("OH").port("a1"))
    with pytest.raises(BadInput, match="no host atom"):
        mol.attach(host, "aZz9", mol.lib("OH").port("a1"))
    with pytest.raises(BadInput, match="port handle"):
        mol.attach(host, "aC1", mol.lib("OH"))


def test_attach_reports_clashes_with_the_validator_rule() -> None:
    # bond=0.6 A drags the fragment into the host: overlap rows, not a raise
    host = mol.frag(smiles="c1ccccc1")
    out = mol.attach(host, _ring_c(host), mol.lib("CH3").port("a1"), bond=0.6)
    assert out.findings
    assert all(f.startswith("attach.clash:") for f in out.findings)


def test_attach_bond_length_override() -> None:
    host = mol.frag(smiles="c1ccccc1")
    c = _ring_c(host)
    out = mol.attach(host, c, mol.lib("OH").port("a1"), bond=1.40)
    o = next(la for la, a in out.scene.atoms.items() if a.element == "O")
    assert float(np.linalg.norm(_cart(out.scene, c) - _cart(out.scene, o))) == (
        pytest.approx(1.40, abs=1e-6)
    )


# -- lib ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "params", "comp", "ports"),
    [
        ("OH", {}, {"O": 1, "H": 1}, ["a1"]),
        ("NH2", {}, {"N": 1, "H": 2}, ["a1"]),
        ("CH3", {}, {"C": 1, "H": 3}, ["a1"]),
        ("COOH", {}, {"C": 1, "O": 2, "H": 1}, ["a1"]),
        ("Ph", {}, {"C": 6, "H": 5}, ["a1"]),
        ("PEG", {"n": 4}, {"C": 8, "O": 5, "H": 17}, ["a1"]),
        ("CH2n", {"n": 3}, {"C": 3, "H": 6}, ["a1", "a2"]),
        ("C2H4", {}, {"C": 2, "H": 4}, ["a1", "a2"]),
        ("c60", {}, {"C": 60}, []),
    ],
)
def test_lib_entries(name: str, params: dict, comp: dict, ports: list[str]) -> None:
    g = mol.lib(name, **params)
    assert g.scene.composition() == comp
    assert sorted(g.ports) == ports


def test_lib_c60_is_the_fullerene_generator() -> None:
    g = mol.lib("c60")
    assert len(g.scene.bonds) == 90
    assert all(a.hybridization == "sp2" for a in g.scene.atoms.values())


def test_lib_refusals() -> None:
    with pytest.raises(BadInput, match="unknown entry") as ei:
        mol.lib("benzyne")
    assert "COOH" in str(ei.value)
    with pytest.raises(BadInput, match="needs n="):
        mol.lib("PEG")
    with pytest.raises(BadInput, match="n must be"):
        mol.lib("CH2n", n=0)
    with pytest.raises(BadInput, match="takes no"):
        mol.lib("OH", n=2)


def test_lib_decorates_with_attach_end_to_end() -> None:
    """A spacer, a group and a ring compose: Ph-CH2CH2-OH."""
    ph = mol.lib("Ph")
    spacer = mol.lib("C2H4")
    a = mol.attach(spacer, spacer.port("a1"), ph.port("a1"))
    b = mol.attach(a, a.port("a2"), mol.lib("OH").port("a1"))
    assert _canonical(b.scene) == _ref("OCCc1ccccc1")


# -- trace -------------------------------------------------------------------------


def _quals(frame) -> set[str]:
    out = {frame.qual}
    for c in frame.children:
        out |= _quals(c)
    return out


def test_atoms_carry_makers_under_a_tracer(tmp_path: Path) -> None:
    with trace.run(work_root=tmp_path) as tr:
        host = mol.frag(smiles="c1ccccc1")
        out = mol.attach(host, _ring_c(host), mol.lib("OH").port("a1"))
        quals = _quals(tr.root)
    assert {"frag", "lib", "attach", "place"} <= quals
    assert all(a.maker is not None for a in out.scene.atoms.values())
    # host and fragment atoms keep distinct creators; the new bond is attach's
    makers = {a.maker for a in out.scene.atoms.values()}
    assert len(makers) >= 2
    assert all(b.maker is not None for b in out.scene.bonds)
