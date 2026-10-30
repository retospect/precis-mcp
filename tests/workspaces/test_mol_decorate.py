"""``mol.sites`` / ``mol.decorate`` / ``mol.cap`` (molecular-workspace-code
slice 2): the site grammar, the torsion scan, the findings and counts, and
the two acceptance anchors — ``cap`` on ``prism_tube(4, 4, 2, '++++++')``
is the ``terminate`` op's function, and ``decorate(sites='open & face:out
& every:3', group='COOH')`` places ⌈n/3⌉ groups with nothing skipped."""

from __future__ import annotations

import hashlib
import math
import subprocess
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rdkit")
from rdkit import Chem

import precis_geom.assembly as pga
import precis_geom.mol as mol
from hexfold.prism import prism_tube
from hexfold.stick import stick
from precis.errors import BadInput
from precis.structure.scene import Scene
from precis_geom import trace
from precis_geom.assembly import Geom, Port
from precis_geom.mol import attachment, decorator
from precis_se.atomic.generators import hexfold_spec

_ROOT = Path(__file__).resolve().parent.parent.parent


def _cart(scene: Scene, label: str) -> np.ndarray:
    return np.asarray(scene.cell.frac_to_cart(scene.atoms[label].frac), dtype=float)


def _sha(points: object) -> str:
    """The goldens' rule: a 1e-6 grid, packed float64."""
    a = np.round(np.asarray(points, dtype=float), 6) + 0.0
    return hashlib.sha256(a.tobytes()).hexdigest()


def _canonical(scene: Scene) -> str:
    rw = Chem.RWMol()
    idx: dict[str, int] = {}
    for la, a in scene.atoms.items():
        at = Chem.Atom(a.element)
        at.SetNoImplicit(True)
        idx[la] = rw.AddAtom(at)
    types = {
        1.0: Chem.BondType.SINGLE,
        1.5: Chem.BondType.AROMATIC,
        2.0: Chem.BondType.DOUBLE,
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


def _positions(g: Geom) -> np.ndarray:
    return np.array([_cart(g.scene, la) for la in g.scene.atoms])


@pytest.fixture(scope="module")
def prism() -> tuple[object, np.ndarray, Geom]:
    net, _tube = prism_tube(4, 4, 2, "++++++")
    xyz = stick(net)
    return net, xyz, pga.geom(net, coords=xyz)


# -- the grammar -----------------------------------------------------------


@pytest.mark.parametrize(
    ("selector", "match", "has_options"),
    [
        ("", "empty", True),
        ("open & bogus", "unknown token", True),
        ("open &", "empty token", True),
        ("face:sideways", "face must be", True),
        ("ring:9-ring", "unknown ring class", True),
        ("element:Carbon", "chemical symbol", False),
        ("every:0", "every wants", False),
        ("every:x", "every wants", False),
        ("frame:", "frame key", False),
    ],
)
def test_selector_refusals_carry_the_accepted_list(
    selector: str, match: str, has_options: bool
) -> None:
    host = mol.frag(smiles="c1ccccc1")
    with pytest.raises(BadInput, match=match) as err:
        mol.sites(host, selector)
    assert err.value.next  # every refusal names the next step
    if has_options:
        assert err.value.options  # and the accepted tokens / values


def test_unknown_token_lists_every_token() -> None:
    with pytest.raises(BadInput) as err:
        mol.sites(mol.frag(smiles="c1ccccc1"), "nope")
    assert err.value.options == [
        "open",
        "face:out|in|axial",
        "ring:<class>",
        "element:<sym>",
        "frame:<key>",
        "every:<k>",
    ]


def test_element_token_names_the_elements_present() -> None:
    with pytest.raises(BadInput) as err:
        mol.sites(mol.frag(smiles="c1ccccc1"), "element:N")
    assert err.value.options == ["C"]


def test_frame_token_needs_a_trace_and_a_known_key(tmp_path: Path) -> None:
    host = mol.frag(smiles="c1ccccc1")
    with pytest.raises(BadInput, match="none is active"):
        mol.sites(host, "frame:abcd1234")
    with trace.run(work_root=tmp_path) as tr:
        g = mol.frag(smiles="c1ccccc1")
        frame = tr.root.children[0]
        assert frame.qual == "frag"
        # the frag frame made every benzene atom
        assert len(mol.sites(g, f"frame:{frame.key}")) == 6
        assert len(mol.sites(g, "frame:qual=frag")) == 6
        with pytest.raises(BadInput, match="no trace frame") as err:
            mol.sites(g, "frame:zzzzzzzz")
        assert frame.key in (err.value.options or [])
        with pytest.raises(BadInput, match="no trace frame"):
            mol.sites(g, "frame:qual=attach")


def test_face_needs_an_axis_it_can_find() -> None:
    # methane is isotropic: no principal axis, so face: refuses naming axis=
    ch4 = mol.cap(mol.lib("CH3"))
    with pytest.raises(BadInput, match="needs an axis") as err:
        mol.sites(ch4, "face:out")
    assert "axis=" in (err.value.next or "")
    # an explicit axis answers
    ch3 = mol.lib("CH3")  # one open valence, on the carbon
    rows = mol.sites(ch3, "open", axis=((0, 0, 0), (0, 0, 1)))
    assert [r.element for r in rows] == ["C"] and rows[0].face in {"out", "in", "axial"}
    faced = mol.sites(ch3, "open & face:" + rows[0].face, axis=((0, 0, 0), (0, 0, 1)))
    assert [r.atom for r in faced] == [rows[0].atom]


def test_sites_describe_atom_direction_face_class() -> None:
    ph = mol.frag(smiles="c1ccccc1")
    rows = mol.sites(ph, "element:C")
    assert len(rows) == 6
    for r in rows:
        assert r.element == "C" and r.atom in ph.scene.atoms
        assert r.ring_class == "open-edge"  # two C neighbours: the H-bearing rim
        assert r.face == "out"  # in-plane, away from the ring's normal axis
        assert r.has_h and r.free == 0
        assert np.linalg.norm(r.direction) == pytest.approx(1.0)
    every2 = mol.sites(ph, "element:C & every:2")
    assert [r.atom for r in every2] == [r.atom for r in rows][::2]
    assert mol.sites(ph, "open") == []  # benzene has nothing open


# -- decorate --------------------------------------------------------------


def test_decorate_pattern_cycles_groups_and_makes_orcinol() -> None:
    ph = mol.frag(smiles="c1ccccc1")
    out = mol.decorate(ph, "element:C & every:2", pattern=["OH", "CH3"])
    # sites 0, 2, 4 -> OH, CH3, OH : 3,5-dihydroxytoluene
    assert _canonical(out.scene) == _ref("Cc1cc(O)cc(O)c1")
    assert out.findings == ()
    assert Counter(a.element for a in out.scene.atoms.values()) == {
        "C": 7,
        "O": 2,
        "H": 8,
    }


def test_decorate_group_forms_agree() -> None:
    ph = mol.frag(smiles="c1ccccc1")
    one = "element:C & every:6"
    by_name = mol.decorate(ph, one, "OH")
    by_smiles = mol.decorate(ph, one, "[*:1]O")
    by_geom = mol.decorate(ph, one, mol.lib("OH"))
    for g in (by_name, by_smiles, by_geom):
        assert _canonical(g.scene) == _ref("Oc1ccccc1")


def test_decorate_refuses_bad_groups_and_arity() -> None:
    ph = mol.frag(smiles="c1ccccc1")
    with pytest.raises(BadInput, match="unknown group") as err:
        mol.decorate(ph, "element:C", "Unobtainium")
    assert {"COOH", "H"} <= set(err.value.options or [])
    with pytest.raises(BadInput, match="exactly one of group= and pattern="):
        mol.decorate(ph, "element:C")
    with pytest.raises(BadInput, match="exactly one of group= and pattern="):
        mol.decorate(ph, "element:C", "OH", pattern=["OH"])
    with pytest.raises(BadInput, match="torsion must be"):
        mol.decorate(ph, "element:C", "OH", torsion="spin")
    with pytest.raises(BadInput, match="no port"):
        mol.decorate(ph, "element:C", mol.lib("c60"))


def test_decorate_saturated_site_is_a_valence_finding() -> None:
    # 'H' on a site that is already H-capped and not open: saturated
    ph = mol.frag(smiles="c1ccccc1")
    out = mol.decorate(ph, "element:C & every:3", "H")
    assert [f.split(":")[0] for f in out.findings] == ["decorate.valence"] * 2
    assert len(out.scene.atoms) == len(ph.scene.atoms)  # nothing placed


def test_decorate_counts_ride_the_trace_frame(tmp_path: Path) -> None:
    with trace.run(work_root=tmp_path) as tr:
        ph = mol.frag(smiles="c1ccccc1")
        mol.decorate(ph, "element:C & every:2", "OH")
        mol.decorate(ph, "element:C & every:3", "H")  # 2 saturated, 0 placed
        first, second = [f for f in tr.root.children if f.qual == "decorate"]
    assert first.made["groups"] == 3 and first.made["atoms"] == 6  # O + H each
    assert first.made["decorate.site_skipped"] == 0
    assert second.made["decorate.valence"] == 2 and second.made["groups"] == 0
    # one attach frame per placed group, under the decorate frame
    assert [c.qual for c in first.children].count("attach") == 3


def test_decorate_skips_a_site_whose_best_torsion_still_overlaps() -> None:
    host = mol.lib("CH3")  # one open valence on the carbon
    # a "group" whose second atom sits 1.2 A along the port direction (toward
    # the host): once the first atom is bonded 1.52 A out, that atom lands
    # 0.32 A from the host carbon whatever the torsion
    base = pga._scene_from_parts(
        ["C", "C"], np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.2]]), []
    )
    first = next(iter(base.atoms))
    port = Port(
        name="a1",
        pos=(0.0, 0.0, 0.0),
        direction=(0.0, 0.0, 1.0),
        atom=first,
        annotations={"order": 1.0},
    )
    group = Geom(scene=base, ports={"a1": port}, meta={"units": "A", "name": "Ptr"})
    out = mol.decorate(host, "open", group)
    assert len(out.scene.atoms) == len(host.scene.atoms)  # nothing placed
    (f,) = out.findings
    assert f.startswith("decorate.site_skipped")
    assert "0.6 × covalent radii" in f


def test_torsion_scan_never_loses_to_a_fixed_torsion() -> None:
    ph = mol.lib("Ph")  # the ipso carbon is open; ortho H sit beside the group
    row = mol.sites(ph, "open")[0]
    cooh = mol.lib("COOH")
    site = Port(name="site", atom=row.atom, direction=row.direction)
    plan = attachment._plan(ph, site, cooh.port("a1"))
    _t, best, hit, _why = decorator._scan(plan, decorator._SCAN, 0, 0)
    assert not hit
    for t in decorator._SCAN:
        _t2, fixed, _hit, _w = decorator._scan(plan, (t,), 0, 0)
        assert fixed <= best + 1e-9
    # a number fixes the torsion: 0 and 90 degrees put the carboxyl O elsewhere
    a = mol.decorate(ph, "open", "COOH", torsion=0.0)
    b = mol.decorate(ph, "open", "COOH", torsion=90.0)
    o_a = next(la for la, at in a.scene.atoms.items() if at.element == "O")
    o_b = next(la for la, at in b.scene.atoms.items() if at.element == "O")
    assert float(np.linalg.norm(_cart(a.scene, o_a) - _cart(b.scene, o_b))) > 0.5


def test_scan_is_deterministic() -> None:
    ph = mol.lib("Ph")
    a = mol.decorate(ph, "open", "COOH", seed=0)
    b = mol.decorate(ph, "open", "COOH", seed=0)
    assert np.array_equal(_positions(a), _positions(b))


# -- cap, plain scene -------------------------------------------------------


def test_cap_plain_scene_closes_every_valence() -> None:
    ch4 = mol.cap(mol.lib("CH3"))
    assert _canonical(ch4.scene) == _ref("C")
    assert mol.sites(ch4, "open") == []
    c = next(la for la, a in ch4.scene.atoms.items() if a.element == "C")
    lengths = [
        float(np.linalg.norm(_cart(ch4.scene, b.j) - _cart(ch4.scene, b.i)))
        for b in ch4.scene.bonds
        if c in (b.i, b.j)
    ]
    assert len(lengths) == 4 and max(lengths) - min(lengths) < 0.05
    # tetrahedral: every H-C-H angle near 109.5
    hs = [
        _cart(ch4.scene, la) - _cart(ch4.scene, c) for la in ch4.scene.atoms if la != c
    ]
    for i in range(4):
        for j in range(i + 1, 4):
            cos = hs[i] @ hs[j] / (np.linalg.norm(hs[i]) * np.linalg.norm(hs[j]))
            assert math.degrees(math.acos(cos)) == pytest.approx(109.5, abs=6.0)


def test_cap_with_another_group() -> None:
    # CH3 + its open valence capped with OH is methanol
    assert _canonical(mol.cap(mol.lib("CH3"), "OH").scene) == _ref("CO")


# -- the Net path -----------------------------------------------------------


def test_prism_geom_keeps_its_net(prism: tuple) -> None:
    net, xyz, g = prism
    ref = g.meta["_net"]
    assert ref.net is net and ref.scene is g.scene
    assert np.array_equal(ref.xyz, xyz)


def test_terminate_open_edges_is_public_with_a_private_alias() -> None:
    assert hexfold_spec._terminate_open_edges is hexfold_spec.terminate_open_edges


def test_cap_on_the_prism_is_the_terminate_ops_function(prism: tuple) -> None:
    net, xyz, g = prism
    hosts, positions = hexfold_spec.terminate_open_edges(net, xyz, "H")
    capped = mol.cap(g)
    n0 = len(g.scene.atoms)
    assert len(capped.scene.atoms) == n0 + len(hosts) == n0 + 144
    new = list(capped.scene.atoms)[n0:]
    assert {capped.scene.atoms[la].element for la in new} == {"H"}
    got = np.array([_cart(capped.scene, la) for la in new])
    assert _sha(got) == _sha(np.array(positions))  # coordinate SHA equal
    assert np.abs(got - np.array(positions)).max() < 1e-9
    labels = list(g.scene.atoms)
    bonded = {
        (b.i, b.j) for b in capped.scene.bonds if capped.scene.atoms[b.j].element == "H"
    }
    assert bonded == {(labels[h], new[k]) for k, h in enumerate(hosts)}
    assert capped.findings == ()


def test_net_cap_atoms_carry_the_cap_frame(prism: tuple, tmp_path: Path) -> None:
    _net, _xyz, g = prism
    with trace.run(work_root=tmp_path) as tr:
        capped = mol.cap(g)
        (frame,) = [f for f in tr.root.children if f.qual == "cap"]
    new = list(capped.scene.atoms)[len(g.scene.atoms) :]
    assert {capped.scene.atoms[la].maker for la in new} == {frame.id}
    assert frame.made["atoms"] == 144 and frame.made["bonds"] == 144
    assert all(b.maker == frame.id for b in capped.scene.bonds[-144:])


#: The sites ``decorate('open & face:out & every:3', 'COOH')`` skips on the
#: prism fixture -- measured, and named by their findings.
_POCKETS = {"aC192", "aC230", "aC251", "aC336", "aC395"}


def _skipped(g: Geom) -> set[str]:
    return {
        f.split()[1].rstrip(":") for f in g.findings if f.startswith("decorate.site_s")
    }


def test_decorate_cooh_every_third_open_out_site(prism: tuple) -> None:
    _net, _xyz, g = prism
    out_sites = mol.sites(g, "open & face:out")
    n = len(out_sites)
    assert n == 56  # measured on this fixture
    chosen = mol.sites(g, "open & face:out & every:3")
    assert len(chosen) == math.ceil(n / 3) == 19
    assert [s.atom for s in chosen] == [s.atom for s in out_sites][::3]
    out = mol.decorate(g, "open & face:out & every:3", group="COOH")
    # the converging-rim pockets: a group there would sit in bonding range of
    # the opposite rim (or of the H its cap puts there) -- skipped, not placed
    assert _skipped(out) == _POCKETS
    assert len(out.findings) == len(_POCKETS)
    placed = len(chosen) - len(_POCKETS)
    assert placed == 14  # ceil(56 / 3) = 19 sites, 5 of them pockets
    els = Counter(a.element for a in out.scene.atoms.values())
    base = Counter(a.element for a in g.scene.atoms.values())
    assert els["C"] - base["C"] == placed and els["O"] == 2 * placed
    assert els["H"] == placed
    # every placed carboxyl carbon is bonded to a chosen host at 1.52 A
    hosts = {s.atom for s in chosen} - _POCKETS
    old = set(g.scene.atoms)
    joins = [
        b
        for b in out.scene.bonds
        if (b.i in hosts and b.j not in old) or (b.j in hosts and b.i not in old)
    ]
    assert len(joins) == placed
    for b in joins:
        d = float(np.linalg.norm(_cart(out.scene, b.i) - _cart(out.scene, b.j)))
        assert d == pytest.approx(1.52, abs=1e-6)
    # a one-valence site that holds a group has nothing left to take
    full = [s for s in chosen if s.free == 1 and s.atom not in _POCKETS][:2]
    assert len(full) == 2
    again = mol.decorate(out, full, "OH")
    new = again.findings[len(out.findings) :]  # findings accumulate on a Geom
    assert [f.split(":")[0] for f in new] == ["decorate.valence"] * 2


def test_cap_after_decorate_keeps_sp2_chemistry(prism: tuple) -> None:
    net, _xyz, g = prism
    decorated = mol.decorate(g, "open & face:out & every:3", "COOH")
    capped = mol.cap(decorated)
    labels = list(g.scene.atoms)
    deg: Counter[str] = Counter()
    for b in capped.scene.bonds:
        deg[b.i] += 1
        deg[b.j] += 1
    # no lattice carbon exceeds its sp2 three bonds: the Net's hybridization
    # (invisible in the all-single-bond scene) steered the caps
    assert max(deg[labels[a.ord]] for a in net.atoms if a.hyb == "sp2") <= 3
    assert len(capped.scene.atoms) > len(decorated.scene.atoms) + 100


def test_cap_after_decorate_takes_the_net_path_on_what_is_left(prism: tuple) -> None:
    """The Net survives decoration: ``cap`` places ``terminate_open_edges``'
    H on every edge atom the decoration did not bond to -- the skipped
    pocket hosts included -- nothing left open, and none beyond the one
    valence a group took."""
    net, xyz, g = prism
    decorated = mol.decorate(g, "open & face:out & every:3", group="COOH")
    capped = mol.cap(decorated)
    # cap adds no skips of its own: only decorate's pocket sites are named
    assert _skipped(capped) == _POCKETS
    assert mol.sites(capped, "open") == []
    labels = list(g.scene.atoms)
    holds_group = {s.atom for s in mol.sites(g, "open & face:out & every:3")} - _POCKETS
    hosts, _pos = hexfold_spec.terminate_open_edges(net, xyz, "H")
    wanted = Counter(labels[h] for h in hosts)  # 2 on a one-bond strip corner
    expect = Counter(
        {
            la: n - (la in holds_group)  # a group took one of the valences
            for la, n in wanted.items()
            if n - (la in holds_group) > 0
        }
    )
    n_new = len(capped.scene.atoms) - len(decorated.scene.atoms)
    assert n_new == sum(expect.values())
    added = set(capped.scene.atoms) - set(decorated.scene.atoms)
    got: Counter[str] = Counter()
    for b in capped.scene.bonds:
        if b.j in added and capped.scene.atoms[b.j].element == "H":
            got[b.i] += 1
    assert got == expect
    # the capped, decorated prism's lattice carbons stay at three bonds
    deg: Counter[str] = Counter()
    for b in capped.scene.bonds:
        deg[b.i] += 1
        deg[b.j] += 1
    assert max(deg[labels[a.ord]] for a in net.atoms) <= 3
    # every pocket host was capped through the Net path
    assert all(got[la] >= 1 for la in _POCKETS)


def test_skipped_pocket_reports_its_bond_range_contact(prism: tuple) -> None:
    _net, _xyz, g = prism
    out = mol.decorate(g, "open & face:out & every:3", group="COOH")
    notes = {f.split()[1].rstrip(":"): f for f in out.findings}
    # aC230's group would sit 1.1 A from the H the cap puts on aC49
    assert "bond-range contact with the cap H of aC49" in notes["aC230"]
    assert all("still leaves a" in f for f in notes.values())


def test_decorated_capped_prism_checks_clean(prism: tuple) -> None:
    from precis.structure import check as chem

    _net, _xyz, g = prism
    capped = mol.cap(mol.decorate(g, "open & face:out & every:3", group="COOH"))
    errors = [f.rule for f in chem.check(capped) if f.severity == "error"]
    assert errors == []


# -- the wheel ----------------------------------------------------------------


def test_site_enumeration_imports_no_server_side_precis() -> None:
    code = (
        "import sys, precis.structure.sites, precis_geom.mol;"
        "bad = [k for k in sys.modules if k.startswith("
        "('precis.store', 'precis.handlers', 'precis.utils.http', 'precis_se.handler'))];"
        "assert not bad, bad"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_ROOT,
        capture_output=True,
        encoding="utf-8",
    )
    assert r.returncode == 0, r.stderr


def test_view_sites_renderer_calls_the_moved_code() -> None:
    from precis.structure import sites as core
    from precis_se.atomic import sites as view

    assert view.ring_classes is core.ring_classes
    assert view._sites_block is core.sites_block
    assert view.Site is core.Site
    assert hasattr(core, "enumerate_sites") and not hasattr(view, "enumerate_sites")
