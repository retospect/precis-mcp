"""NEB endpoint authoring (gr512126, slice 1): the hydrocarbon adsorbate
groups (C/CH/CH2/CH3/CH4) and the ``transfer``/``move_group``
reaction-endpoint ops.

Kernel-level tests are DB-free ``Scene`` + ``apply_ops`` in the
``test_structure_kernel.py`` style: the CHx catalogue geometry is checked
on a toy anchor scaffold, the ops on small molecule-mode scenes, and the
slice's acceptance pair — Cu(111)[3,3,4] ``fix_layers:2`` + a bridging O +
a top-site CH4, the FS derived by ``transfer`` + ``move_group`` — is built
pure and run through ``validate`` + ``preflight`` (zero errors, identical
label sets/cell/fixed flags between IS and FS). The same pair on Pd(111)
with one surface Cu (``set_element``) covers comment 20's PdCu ask and the
largest-connected-metal-cluster slab rule.
"""

from __future__ import annotations

import copy
import json
import re

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.structure import StructureHandler
from precis.structure import OpError, Scene, apply_ops, preflight, probe, validate
from precis.structure import ops as structure_ops
from precis.structure.cell import Cell
from precis.structure.elements import covalent_radius


def _molecule_cell(size: float = 20.0) -> Cell:
    return Cell(np.eye(3) * size, pbc=(False, False, False))


def _pd_triangle() -> Scene:
    """Three Pd anchors in a flat triangle — the toy site scaffold
    ``test_structure_kernel`` uses for site-symbolic placement."""
    scene = Scene(cell=Cell(np.eye(3) * 20.0))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "cart": [2.75, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "cart": [1.375, 2.38, 0.0]},
        ],
    )
    return scene


def _errors(findings) -> list:
    return [f for f in findings if f.severity == "error"]


# -- CHx adsorbate catalogue rows -----------------------------------------


def _place(species: str, height: float = 2.0) -> Scene:
    scene = _pd_triangle()
    apply_ops(
        scene,
        [
            {
                "op": "add_adsorbate",
                "species": species,
                "site": {"type": "top", "anchors": ["aPd1"]},
                "height": height,
            }
        ],
    )
    return scene


def _group(scene: Scene) -> list[tuple[str, str, np.ndarray]]:
    """The non-anchor atoms, in mint order, as (label, element, cart)."""
    return [
        (la, a.element, scene.cell.frac_to_cart(a.frac))
        for la, a in scene.atoms.items()
        if not la.startswith("aPd")
    ]


def _mic_cart(scene: Scene, centre: np.ndarray, label: str) -> np.ndarray:
    """``label``'s cartesian position on the periodic image nearest
    ``centre`` — the MIC unwrap, for distance/direction asserts."""
    _d, img = scene.cell.mic(scene.cell.cart_to_frac(centre), scene.atoms[label].frac)
    return scene.cell.frac_to_cart(
        scene.atoms[label].frac + np.asarray(img, dtype=float)
    )


def test_add_adsorbate_ch4_tetrahedral_geometry() -> None:
    scene = _place("CH4", height=2.5)
    placed = _group(scene)
    assert [el for _la, el, _ in placed] == ["C", "H", "H", "H", "H"]
    c = placed[0][2]
    hs = [(la, _mic_cart(scene, c, la)) for la, _el, _ in placed[1:]]
    # The binding C lands exactly on the named top site.
    assert c == pytest.approx([0.0, 0.0, 2.5], abs=1e-6)
    # Every C-H is ~1.09 Å; one H points DOWN at the surface (the
    # physisorbed guess), the other three form the tetrahedral cap.
    for _la, h in hs:
        assert float(np.linalg.norm(h - c)) == pytest.approx(1.09, abs=2e-3)
    z_below = [h for _la, h in hs if h[2] < c[2]]
    z_above = [h for _la, h in hs if h[2] > c[2]]
    assert len(z_below) == 1 and len(z_above) == 3
    assert z_below[0][2] == pytest.approx(2.5 - 1.09, abs=1e-6)
    # Tetrahedral: every C-H pair makes the 109.47° angle.
    for i in range(len(hs)):
        for j in range(i + 1, len(hs)):
            v1, v2 = hs[i][1] - c, hs[j][1] - c
            cos = float(v1 @ v2 / (np.linalg.norm(v1) * np.linalg.norm(v2)))
            assert np.degrees(np.arccos(cos)) == pytest.approx(109.47, abs=0.1)
    # Four declared intra-group bonds, all on the C; nothing to the slab.
    assert len(scene.bonds) == 4
    for bond in scene.bonds:
        assert "aC1" in (bond.i, bond.j)
        assert bond.provenance == "declared"


def test_add_adsorbate_ch3_ch2_ch_c_rows() -> None:
    """The dehydrogenation series: C-only to CH3, H's all UP and away from
    the C-surface bond, each with its declared C-H bonds minted."""
    for species, n_h in [("C", 0), ("CH", 1), ("CH2", 2), ("CH3", 3)]:
        scene = _place(species, height=2.0)
        placed = _group(scene)
        assert [el for _la, el, _ in placed] == ["C"] + ["H"] * n_h
        c = placed[0][2]
        assert c == pytest.approx([0.0, 0.0, 2.0], abs=1e-6)
        for la, _el, _cart in placed[1:]:
            h = _mic_cart(scene, c, la)
            assert h[2] > c[2]  # H's up, away from the surface bond
            assert float(np.linalg.norm(h - c)) == pytest.approx(1.1, abs=0.03)
        assert len(scene.bonds) == n_h


# -- transfer --------------------------------------------------------------


def _ch_oh_scene() -> Scene:
    """A donor C-H and an O-H acceptor, 5 Å apart, molecule-mode."""
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "O", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "cart": [0.97, 0.0, 0.0]},
            {"op": "add_bond", "i": "aO1", "j": "aH1", "order": 1},
            {"op": "add_atom", "element": "C", "cart": [5.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "cart": [6.09, 0.0, 0.0]},
            {"op": "add_bond", "i": "aC1", "j": "aH2", "order": 1},
        ],
    )
    return scene


def test_transfer_atom_moves_onto_acceptor_and_rebonds() -> None:
    scene = _ch_oh_scene()
    apply_ops(scene, [{"op": "transfer", "atom": "aH2", "to": "aO1"}])
    # Lands at the covalent-radius sum along the acceptor's open
    # coordination direction (opposite its bonded aH1): O + 0.97*(-x).
    h_cart = scene.cell.frac_to_cart(scene.atoms["aH2"].frac)
    assert h_cart == pytest.approx(
        [-(covalent_radius("O") + covalent_radius("H")), 0.0, 0.0], abs=1e-6
    )
    # The moved atom's old declared bond is gone; atom-to is minted.
    pairs = {frozenset((b.i, b.j)) for b in scene.bonds}
    assert frozenset(("aC1", "aH2")) not in pairs
    assert frozenset(("aO1", "aH2")) in pairs
    new_bond = next(b for b in scene.bonds if {b.i, b.j} == {"aO1", "aH2"})
    assert new_bond.provenance == "declared" and new_bond.order == 1.0
    # The acceptor's own bond is untouched.
    assert frozenset(("aO1", "aH1")) in pairs


def test_transfer_from_picks_nearest_and_writes_the_pick_back() -> None:
    scene = _ch_oh_scene()
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "H", "cart": [3.91, 0.0, 0.0]},
            {"op": "add_bond", "i": "aC1", "j": "aH3", "order": 1},
        ],
    )
    op: dict = {"op": "transfer", "from": "aC1", "to": "aO1"}
    apply_ops(scene, [op])
    # aH3 sits ~1.2 Å nearer aO1 than aH2 — the pick is written into the op
    # so the stored revision names the one reading taken.
    assert op["atom"] == "aH3"
    pairs = {frozenset((b.i, b.j)) for b in scene.bonds}
    assert frozenset(("aO1", "aH3")) in pairs
    assert frozenset(("aC1", "aH2")) in pairs  # the other H stayed


def test_transfer_from_equidistant_tie_refuses_with_options() -> None:
    """Two declared-bonded candidates equidistant from the acceptor (within
    0.05 Å): refuse, both labels in ``options=`` (rule 9: one reading)."""
    scene = Scene(cell=_molecule_cell())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "cart": [1.09, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "cart": [-1.09, 0.0, 0.0]},
            {"op": "add_bond", "i": "aC1", "j": "aH1", "order": 1},
            {"op": "add_bond", "i": "aC1", "j": "aH2", "order": 1},
            {"op": "add_atom", "element": "O", "cart": [0.0, 2.0, 0.0]},
        ],
    )
    with pytest.raises(OpError) as exc:
        apply_ops(scene, [{"op": "transfer", "from": "aC1", "to": "aO1"}])
    assert exc.value.options is not None
    assert sorted(exc.value.options) == ["aH1", "aH2"]
    assert "atom" in str(exc.value)  # tells the caller how to pick


def test_transfer_refuses_a_fixed_moved_atom() -> None:
    scene = _ch_oh_scene()
    apply_ops(scene, [{"op": "constrain", "atoms": ["aH2"], "kind": "fixed-all"}])
    with pytest.raises(OpError, match="constrained|fixed"):
        apply_ops(scene, [{"op": "transfer", "atom": "aH2", "to": "aO1"}])


def test_transfer_needs_exactly_one_of_atom_or_from() -> None:
    scene = _ch_oh_scene()
    for op in (
        {"op": "transfer", "atom": "aH2", "from": "aC1", "to": "aO1"},
        {"op": "transfer", "to": "aO1"},
        {"op": "transfer", "atom": "aH2"},
    ):
        with pytest.raises(OpError):
            apply_ops(scene, [op])


# -- move_group -----------------------------------------------------------


def _ch2_over_pd() -> Scene:
    """A CH2 fragment ({aC1,aH1,aH2} declared-bonded) away from a Pd anchor
    in a periodic cell."""
    scene = Scene(cell=Cell(np.eye(3) * 20.0))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "C", "cart": [5.0, 5.0, 5.0]},
            {"op": "add_atom", "element": "H", "cart": [5.0, 5.9, 5.5]},
            {"op": "add_atom", "element": "H", "cart": [5.0, 4.1, 5.5]},
            {"op": "add_bond", "i": "aC1", "j": "aH1", "order": 1},
            {"op": "add_bond", "i": "aC1", "j": "aH2", "order": 1},
        ],
    )
    return scene


def test_move_group_moves_the_whole_fragment_rigidly() -> None:
    scene = _ch2_over_pd()
    before = {la: a.frac.copy() for la, a in scene.atoms.items()}
    apply_ops(
        scene,
        [
            {
                "op": "move_group",
                "atom": "aC1",
                "site": {"type": "top", "anchors": ["aPd1"]},
                "height": 2.0,
            }
        ],
    )
    after = {la: a.frac.copy() for la, a in scene.atoms.items()}

    def _disp(la: str) -> np.ndarray:
        """The MIC cartesian vector this atom moved by."""
        _d, img = scene.cell.mic(before[la], after[la])
        return scene.cell.frac_to_cart(
            after[la] + np.asarray(img, dtype=float) - before[la]
        )

    # The C lands on the named top site; every fragment atom took the SAME
    # cartesian delta (the move is rigid — internal geometry untouched).
    assert scene.cell.frac_to_cart(after["aC1"]) == pytest.approx(
        [0.0, 0.0, 2.0], abs=1e-6
    )
    delta = _disp("aC1")
    for la in ("aH1", "aH2"):
        assert _disp(la) == pytest.approx(delta, abs=1e-6)
    assert probe.distance(scene, "aC1", "aH1") == pytest.approx(
        float(np.linalg.norm(np.array([0.0, 0.9, 0.5]))), abs=1e-6
    )
    assert scene.cell.frac_to_cart(after["aPd1"]) == pytest.approx(
        [0.0, 0.0, 0.0], abs=1e-6
    )


def test_move_group_refuses_a_fragment_with_a_fixed_atom() -> None:
    scene = _ch2_over_pd()
    apply_ops(scene, [{"op": "constrain", "atoms": ["aC1"], "kind": "fixed-all"}])
    with pytest.raises(OpError, match="fixed"):
        apply_ops(
            scene,
            [
                {
                    "op": "move_group",
                    "atom": "aH1",
                    "site": {"type": "top", "anchors": ["aPd1"]},
                }
            ],
        )


def test_move_group_refuses_a_fragment_touching_the_slab() -> None:
    """The slab (largest connected metal component over combined adjacency)
    does not move — a fragment declared-bonded to it refuses."""
    scene = _ch2_over_pd()
    apply_ops(
        scene,
        [{"op": "add_bond", "i": "aC1", "j": "aPd1", "order": 1}],
    )
    with pytest.raises(OpError, match="slab"):
        apply_ops(
            scene,
            [
                {
                    "op": "move_group",
                    "atom": "aC1",
                    "site": {"type": "top", "anchors": ["aPd1"]},
                }
            ],
        )


def test_move_group_refuses_a_slab_atom_directly() -> None:
    scene = _pd_triangle()
    with pytest.raises(OpError, match="slab"):
        apply_ops(
            scene,
            [
                {
                    "op": "move_group",
                    "atom": "aPd1",
                    "site": {"type": "top", "anchors": ["aPd2"]},
                }
            ],
        )


# -- the acceptance pair: Cu(111)/O/CH4 IS -> FS ---------------------------

_CH4_HEIGHT = 2.5  # physisorbed guess: H-down sits ~1.4 Å over the surface
_CH3_HEIGHT = 2.4
_O_HEIGHT = 1.9


def _top_layer(scene: Scene) -> dict[str, np.ndarray]:
    """Label -> cart for the slab's top layer (max z within a layer gap)."""
    carts = {la: scene.cell.frac_to_cart(a.frac) for la, a in scene.atoms.items()}
    z_top = max(c[2] for c in carts.values())
    return {la: c for la, c in carts.items() if c[2] > z_top - 0.5}


def _mic(scene: Scene, a: str, b: str) -> float:
    return scene.cell.mic(scene.atoms[a].frac, scene.atoms[b].frac)[0]


def _bridge_pair(scene: Scene, top: dict[str, np.ndarray]) -> tuple[str, str]:
    """The nearest top-layer pair — a real fcc(111) bridge site."""
    labels = sorted(top)
    best: tuple[float, str, str] | None = None
    for i, la in enumerate(labels):
        for lb in labels[i + 1 :]:
            d = _mic(scene, la, lb)
            if best is None or d < best[0]:
                best = (d, la, lb)
    assert best is not None
    return best[1], best[2]


def _near_far_top(
    scene: Scene, top: dict[str, np.ndarray], cu_a: str, cu_b: str
) -> tuple[str, str]:
    """(top-site atom neighbouring the bridge, the farthest top atom from
    it) — the CH4 site and the move_group landing site."""
    rest = [la for la in top if la not in (cu_a, cu_b)]
    near = min(rest, key=lambda la: _mic(scene, la, cu_a) + _mic(scene, la, cu_b))
    far = max(rest, key=lambda la: min(_mic(scene, la, cu_a), _mic(scene, la, cu_b)))
    return near, far


def _endpoint_is(element: str = "Cu") -> tuple[Scene, str, str]:
    """The IS: a [3,3,4] fcc(111) slab, bottom two layers fixed, O on a
    bridge site and CH4 on a neighbouring top site — built with ``slab`` +
    ``add_adsorbate`` only. Returns (scene, move_group target anchor, a
    spare top-layer label — e.g. the dopant site)."""
    pytest.importorskip("ase")
    scene = Scene(cell=Cell(np.eye(3)))
    apply_ops(
        scene,
        [
            {
                "op": "slab",
                "element": element,
                "size": [3, 3, 4],
                "vacuum": 12.0,
                "fix_layers": 2,
            }
        ],
    )
    top = _top_layer(scene)
    assert len(top) == 9
    cu_a, cu_b = _bridge_pair(scene, top)
    ch4_site, mg_site = _near_far_top(scene, top, cu_a, cu_b)
    spare = next(la for la in sorted(top) if la not in (cu_a, cu_b, ch4_site, mg_site))
    apply_ops(
        scene,
        [
            {
                "op": "add_adsorbate",
                "species": "O",
                "site": {"type": "bridge", "anchors": [cu_a, cu_b]},
                "height": _O_HEIGHT,
            },
            {
                "op": "add_adsorbate",
                "species": "CH4",
                "site": {"type": "top", "anchors": [ch4_site]},
                "height": _CH4_HEIGHT,
            },
        ],
    )
    return scene, mg_site, spare


def _fs_ops(mg_site: str) -> list[dict]:
    """CH4 + O* -> CH3* + OH*: transfer an H from C onto O, then move the
    remaining CH3 fragment to a fresh top site. ``direction`` is only the
    fallback for an acceptor with no usable neighbours (ignored then)."""
    return [
        {
            "op": "transfer",
            "from": "aC1",
            "to": "aO1",
            "direction": [0.0, 0.0, 1.0],
        },
        {
            "op": "move_group",
            "atom": "aC1",
            "site": {"type": "top", "anchors": [mg_site]},
            "height": _CH3_HEIGHT,
        },
    ]


def _assert_pair(scene_is: Scene, scene_fs: Scene) -> None:
    """Both endpoints validate/preflight clean and stay label-, cell- and
    fixed-flag-identical — the NEB-pairable contract."""
    assert set(scene_fs.atoms) == set(scene_is.atoms)
    for la in scene_is.atoms:
        a, b = scene_is.atoms[la], scene_fs.atoms[la]
        assert a.element == b.element and a.fixed == b.fixed
    assert np.allclose(scene_is.cell.lattice, scene_fs.cell.lattice)
    assert scene_is.cell.pbc == scene_fs.cell.pbc
    assert _errors(validate(scene_is)) == []
    assert _errors(validate(scene_fs)) == []
    assert preflight.preflight(scene_is).ok
    assert preflight.preflight(scene_fs).ok


def _assert_reaction_done(scene_fs: Scene) -> None:
    """OH* formed (one aH–aO1 bond), CH3* left (aC1 on three H's)."""
    pairs = {frozenset((b.i, b.j)) for b in scene_fs.bonds}
    assert any(
        frozenset((la, "aO1")) in pairs for la in scene_fs.atoms if la.startswith("aH")
    )
    c_bonds = [b for b in scene_fs.bonds if "aC1" in (b.i, b.j)]
    assert len(c_bonds) == 3


def test_cu111_o_ch4_is_validates_then_derives_a_pairable_fs() -> None:
    scene_is, mg_site, _spare = _endpoint_is("Cu")
    scene_fs = copy.deepcopy(scene_is)
    apply_ops(scene_fs, _fs_ops(mg_site))
    _assert_reaction_done(scene_fs)
    _assert_pair(scene_is, scene_fs)


def test_pd111_with_surface_cu_dopant_builds_the_same_pair() -> None:
    """comment 20's PdCu ask: one surface Cu via set_element still validates,
    and the dopant lands inside the slab (largest connected metal cluster),
    so move_group's slab rule treats it as slab, not adsorbate."""
    pytest.importorskip("ase")
    scene_is, mg_site, dopant = _endpoint_is("Pd")
    apply_ops(
        scene_is,
        [{"op": "set_element", "atom": dopant, "element": "Cu"}],
    )
    slab = structure_ops._largest_metal_component(
        scene_is, structure_ops._combined_adjacency(scene_is)
    )
    assert dopant in slab  # a dopant IS slab, whatever element dominates
    assert len(slab) == 36
    scene_fs = copy.deepcopy(scene_is)
    apply_ops(scene_fs, _fs_ops(mg_site))
    _assert_reaction_done(scene_fs)
    _assert_pair(scene_is, scene_fs)


def test_slab_op_skill_examples_dry_build() -> None:
    """gr520132: the slab skill's slab-op H2 carries two worked
    examples (an Ni adatom on Cu(111); an Ni single-atom alloy + a
    bridging O). Parse their ``text='{…}'`` payloads back out of the
    skill file and dry-build each — a schema drift (a renamed arg, a dead
    site type, a wrong anchor count) fails here instead of in the next
    quest tick."""
    pytest.importorskip("ase")
    from tests._skill_roots import skill_path

    text = skill_path("precis-structure-slab-help").read_text(encoding="utf-8")
    section = text.split("## Build a slab", 1)[1].split("\n## ", 1)[0]
    payloads = [
        json.loads(m) for m in re.findall(r"text='(\{.*?\})'", section, re.DOTALL)
    ]
    assert len(payloads) == 2
    adatom, saa = payloads
    # The Ni adatom: aNi1 minted on the aCu28 top site.
    scene = Scene(cell=Cell(np.eye(3) * 20.0))
    apply_ops(scene, adatom["ops"])
    assert len(scene.atoms) == 37
    assert scene.atoms["aNi1"].element == "Ni"
    # The Ni SAA + bridging O: set_element keeps the aCu28 label — the
    # dopant IS aCu28 — and aO1 lands over the aCu28–aCu29 bridge.
    scene = Scene(cell=Cell(np.eye(3) * 20.0))
    apply_ops(scene, saa["ops"])
    assert len(scene.atoms) == 37
    assert scene.atoms["aCu28"].element == "Ni"
    assert scene.atoms["aO1"].element == "O"
    assert _errors(validate(scene)) == []


# -- the derive_from door (handler-level, db fixture) ---------------------
#
# put(id='<new-slug>', args={'derive_from': '<parent>', 'ops': [...]}) is
# the MCP door onto StructureHandler.derive — branch a design keeping the
# parent's atom labels so the child stays NEB-pairable.

#: A tiny clean design (validate hard-gates put): C–H + a floating O the
#: derive-time ``transfer`` can pull the H onto.
_IS_PAYLOAD = json.dumps(
    {
        "cell": {"a": 20.0, "b": 20.0, "c": 20.0, "pbc": [False, False, False]},
        "ops": [
            {"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "H", "frac": [0.5545, 0.5, 0.5]},
            {"op": "add_bond", "i": "aC1", "j": "aH1", "order": 1},
            {"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.65]},
        ],
    }
)

_TRANSFER_OP = {
    "op": "transfer",
    "atom": "aH1",
    "to": "aO1",
    "direction": [0, 0, 1],
}


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


def test_derive_from_ack_reports_labels_preserved(structure) -> None:
    structure.put(id="ch_is", text=_IS_PAYLOAD)
    resp = structure.put(
        id="ch_fs",
        args={"derive_from": "ch_is", "ops": [dict(_TRANSFER_OP)]},
    )
    assert "labels preserved" in resp.body
    assert "NEB-pairable" in resp.body
    # The FS really did the bond-swap — aH1 is declared-bonded to aO1.
    bonds = structure.get(id="ch_fs", view="bonds")
    assert "aH1" in bonds.body and "aO1" in bonds.body


def test_derive_from_atom_set_change_reports_not_pairable(structure) -> None:
    structure.put(id="ch_is", text=_IS_PAYLOAD)
    resp = structure.put(
        id="ch_fs",
        args={"derive_from": "ch_is", "ops": [{"op": "vacancy", "atom": "aH1"}]},
    )
    assert "atom set changed" in resp.body
    assert "not NEB-pairable" in resp.body


def test_derive_from_existing_slug_nexts_dash_2(structure) -> None:
    structure.put(id="ch_is", text=_IS_PAYLOAD)
    structure.put(id="ch_fs", args={"derive_from": "ch_is", "ops": []})
    with pytest.raises(BadInput) as exc:
        structure.put(id="ch_fs", args={"derive_from": "ch_is", "ops": []})
    assert "ch_fs-2" in str(exc.value.next)


def test_derive_from_requires_id_and_parent(structure) -> None:
    structure.put(id="ch_is", text=_IS_PAYLOAD)
    with pytest.raises(BadInput):
        structure.put(args={"derive_from": "ch_is"})
    with pytest.raises(BadInput):
        structure.put(id="ch_fs", args={"derive_from": "  "})
    with pytest.raises(NotFound):
        structure.put(id="ch_fs", args={"derive_from": "no_such_parent"})
