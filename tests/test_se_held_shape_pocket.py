"""pocket-from-molecule slice 3 (docs/backlog/pocket-from-molecule.md):
the held-shape pocket — ``pocket_from_molecule`` with ``remove=`` designs
for NH3 minus one H, the NH2 frozen at the NH3 geometry.

GFN2 is stubbed by ``_HarmonicTBLite``: harmonic N–H and H···H springs
whose rest lengths give NH2 a narrower H–N–H angle (≈102°) than NH3's
106.7°, so the frozen fragment is strained and the free relax moves the
H atoms. The real-backend case skips without tblite.
"""

from __future__ import annotations

import json
import math
import sys
import types
from typing import Any

import numpy as np
import pytest
from ase.calculators.calculator import Calculator, all_changes

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis.structure.cell import Cell
from precis.structure.scene import Atom as StructAtom
from precis.structure.scene import Scene as StructScene
from precis_se.handler import SeHandler
from tests.test_se_pocket_from_molecule import _run_migration

_NH = 1.012
_HNH_DEG = 106.7


def _nh3() -> np.ndarray:
    """NH3 with N at the origin, the three H below it at 120° azimuths."""
    cos_a = math.cos(math.radians(_HNH_DEG))
    cos_t = math.sqrt((cos_a + 0.5) / 1.5)
    sin_t = math.sqrt(1.0 - cos_t * cos_t)
    hs = [
        [_NH * sin_t * math.cos(phi), _NH * sin_t * math.sin(phi), -_NH * cos_t]
        for phi in (0.0, 2 * math.pi / 3, 4 * math.pi / 3)
    ]
    return np.array([[0.0, 0.0, 0.0], *hs])


_NH3 = _nh3()
_REMOVED = "aH3"  # labels are minted aN1, aH1, aH2, aH3 in order
_VACANCY = (_NH3[3] - _NH3[0]) / np.linalg.norm(_NH3[3] - _NH3[0])


class _HarmonicTBLite(Calculator):
    """TBLite stand-in: springs N–H (rest 1.03 Å) and H···H (rest 1.60 Å),
    fixed charges N −0.3 e / H +0.1 e (mean-shifted to neutral)."""

    implemented_properties = ["energy", "free_energy", "forces", "charges"]
    seen: list[dict[str, Any]] = []
    _REST = {frozenset(("N", "H")): 1.03, frozenset(("H",)): 1.60}

    def __init__(self, atoms=None, **kwargs: Any):
        super().__init__(atoms=atoms)
        _HarmonicTBLite.seen.append(kwargs)

    def calculate(
        self, atoms=None, properties=("energy",), system_changes=all_changes
    ) -> None:
        super().calculate(atoms, properties, system_changes)
        assert self.atoms is not None
        pos = self.atoms.get_positions()
        sym = self.atoms.get_chemical_symbols()
        n = len(sym)
        energy = 0.0
        forces = np.zeros((n, 3))
        for i in range(n):
            for j in range(i + 1, n):
                rest = self._REST.get(frozenset((sym[i], sym[j])))
                d = pos[j] - pos[i]
                r = float(np.linalg.norm(d))
                if rest is None or r > 2.5:
                    continue
                energy += 10.0 * (r - rest) ** 2
                f = 20.0 * (r - rest) * d / r
                forces[i] += f
                forces[j] -= f
        q = np.array([-0.3 if s == "N" else 0.1 if s == "H" else 0.0 for s in sym])
        self.results = {
            "energy": energy,
            "free_energy": energy,
            "forces": forces,
            "charges": q - q.mean(),
        }


def _stub_tblite(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("tblite.ase")
    setattr(fake, "TBLite", _HarmonicTBLite)
    monkeypatch.setitem(sys.modules, "tblite.ase", fake)
    monkeypatch.setattr(_HarmonicTBLite, "seen", [])


def _save(store: Store, slug: str, elements: list[str], xyz: np.ndarray) -> None:
    """Stored at +10 Å in a 20 Å non-periodic box, like slice 2's fixture."""
    cell = Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False))
    scene = StructScene(cell=cell)
    for el, p in zip(elements, xyz, strict=True):
        label = scene.next_label(el)
        scene.atoms[label] = StructAtom(
            label, el, cell.cart_to_frac(np.asarray(p) + 10.0)
        )
    store.structure_save(slug=slug, title=slug, scene=scene, version=1, card_text="t.")


def _wall_ring(extra: list[np.ndarray] | None = None) -> np.ndarray:
    """Six carbons on a 4 Å ring around N in the z = 0 plane, plus extras."""
    ring = [
        [4.0 * math.cos(k * math.pi / 3), 4.0 * math.sin(k * math.pi / 3), 0.0]
        for k in range(6)
    ]
    return np.array(ring + [list(e) for e in (extra or [])])


def _design(store: Store, slug: str, wall_xyz: np.ndarray) -> SeHandler:
    _save(store, f"{slug}-wall", ["C"] * len(wall_xyz), wall_xyz)
    _save(store, f"{slug}-nh3", ["N", "H", "H", "H"], _NH3)
    se = SeHandler(hub=Hub(store=store))
    ops = [
        {"op": "add_block", "name": "wall", "envelope": "sphere:r1e-9"},
        {"op": "bind_structure", "block": "wall", "design": f"{slug}-wall"},
        {"op": "add_block", "name": "nh3", "envelope": "sphere:r3e-10"},
        {"op": "bind_structure", "block": "nh3", "design": f"{slug}-nh3"},
    ]
    se.put(id=slug, text=json.dumps({"ops": ops}))
    return se


_HELD = {
    "op": "pocket_from_molecule",
    "molecule": "nh3",
    "wall": "wall",
    "z_window": [0.5, 1.5],
    "remove": _REMOVED,
}


def _tree(handler: SeHandler, slug: str) -> Any:
    from precis_se import persist

    ref = handler.store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(handler.store, ref.id)


def _pocket(tree: Any) -> Any:
    (pocket,) = tree.blocks["wall"].pockets.values()
    return pocket


def _measures(tree: Any, selector: str) -> list[Any]:
    from precis_se.pockets import region_measures

    return region_measures(tree, "wall", selector)


@pytest.fixture
def taxonomy(store: Store) -> Store:
    """The measurand taxonomy incl. 0228's binding energy."""
    _run_migration("0174_taxon_seed.sql")
    _run_migration("0182_se_measurand_seed.sql")
    _run_migration("0228_se_measurands_esp_binding.sql")
    return store


@pytest.fixture
def held(store: Store, taxonomy: Store, monkeypatch: pytest.MonkeyPatch) -> SeHandler:
    _stub_tblite(monkeypatch)
    return _design(store, "held", _wall_ring())


def test_strain_is_positive_and_vacancy_follows_the_removed_bond(
    held: SeHandler,
) -> None:
    from precis_se.properties.pocket_spec import held_shape

    shape = held_shape(_tree(held, "held"), "nh3", _REMOVED)
    assert shape.anchor == "aN1"
    assert (shape.charge, shape.multiplicity) == (0, 2)  # NH2: a doublet
    assert shape.strain_eV > 0.0
    assert shape.relax_converged
    cos = float(np.dot(shape.vacancy_world, _VACANCY))
    assert math.degrees(math.acos(min(1.0, cos))) < 1.0
    # both H atoms move when freed (N may too, after superposition); both
    # H are donors
    assert {"aH1", "aH2"} <= {label for label, _d, _x in shape.holds}
    assert sorted(label for label, _d in shape.donors) == ["aH1", "aH2"]


def test_held_pocket_carries_keep_open_cone_and_binding_requirement(
    held: SeHandler,
) -> None:
    from precis_se.datums import parse_selector

    held.edit(id="held", ops=[_HELD])
    tree = _tree(held, "held")
    pocket = _pocket(tree)
    assert pocket.name.startswith("wall-nh3!aH3@z5-15")
    assert pocket.shape == "mol:nh3!aH3@0.0"
    sels = {r: parse_selector(r) for r in pocket.regions}
    assert any(s.kind == "mpatch" and s.without == _REMOVED for s in sels.values())

    by_name = {m.name.split("/")[-1]: m for m in tree.measures if m.datum}
    keep_open = by_name["open"].datum
    assert sels[keep_open].kind == "mcone" and sels[keep_open].anchor == "aN1"
    axis = np.asarray(sels[keep_open].axis)
    assert float(np.dot(axis / np.linalg.norm(axis), _VACANCY)) > math.cos(
        math.radians(1.0)
    )
    (m_open,) = _measures(tree, keep_open)
    assert (m_open.unit, m_open.max_value) == ("count", 0)

    (m_bind,) = _measures(tree, "mol:nh3!aH3@0.0")
    assert m_bind.measurand == "binding-energy"
    from precis_se.properties.pocket_spec import held_shape

    strain = held_shape(tree, "nh3", _REMOVED).strain_eV
    assert m_bind.max_value == pytest.approx(-strain, abs=1e-4)
    assert "open question 11" in (m_bind.reason or "")

    for label in ("aH1", "aH2"):
        hold, hbond = by_name[f"hold_{label}"], by_name[f"hbond_{label}"]
        assert sels[hold.datum].anchor == label == sels[hbond.datum].anchor
        assert (hold.unit, hold.min_value) == ("count", 1)
        assert (hbond.measurand, hbond.min_value) == ("h-bond-acceptor-count", 1)


def test_held_pocket_round_trips_through_view_ops(held: SeHandler) -> None:
    held.edit(id="held", ops=[_HELD])
    pocket = _pocket(_tree(held, "held"))
    body = held.get(id="held", view="ops").body
    exported = json.loads(body.split("```json")[1].split("```")[0])["ops"]
    held.put(id="held-copy", text=json.dumps({"ops": exported}))
    copy = _pocket(_tree(held, "held-copy"))
    assert copy.regions == pocket.regions
    assert copy.class_hash == pocket.class_hash


def test_clean_vacancy_has_no_keep_open_finding(held: SeHandler) -> None:
    held.edit(id="held", ops=[_HELD])
    body = held.get(id="held", view="drc").body
    assert "pocket_keep_open" not in body
    pockets = held.get(id="held", view="pockets").body
    assert "0 wall atom(s) in cone" in pockets


def test_wall_atom_inside_the_cone_is_a_drc_error_naming_the_region(
    store: Store, taxonomy: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_tblite(monkeypatch)
    blocker = 2.0 * _VACANCY  # 2 Å out along the removed N–H
    se = _design(store, "blocked", _wall_ring([blocker]))
    se.edit(id="blocked", ops=[_HELD])
    pocket = _pocket(_tree(se, "blocked"))
    keep_open = next(r for r in pocket.regions if r.startswith("mcone:nh3!aH3:aN1@"))
    body = se.get(id="blocked", view="drc").body
    lines = [line for line in body.splitlines() if "pocket_keep_open" in line]
    assert lines, body
    assert keep_open in lines[0] and pocket.name in lines[0]
    assert "error" in lines[0].lower()


def test_spin_polarization_is_off_by_default_and_explicit_when_on(
    held: SeHandler,
) -> None:
    from precis_se.properties.pocket_spec import held_shape

    tree = _tree(held, "held")
    held_shape(tree, "nh3", _REMOVED)
    assert _HarmonicTBLite.seen
    assert all("spin_polarization" not in kw for kw in _HarmonicTBLite.seen)
    _HarmonicTBLite.seen.clear()
    on = held_shape(tree, "nh3", _REMOVED, spin_polarization=True)
    assert on.spin_polarization
    assert _HarmonicTBLite.seen
    assert all(kw.get("spin_polarization") == 1.0 for kw in _HarmonicTBLite.seen)


def test_held_args_are_vetted(held: SeHandler) -> None:
    with pytest.raises(BadInput, match="no atom 'aH9'"):
        held.edit(id="held", ops=[{**_HELD, "remove": "aH9"}])
    with pytest.raises(BadInput, match="formal-charge sum"):
        held.edit(id="held", ops=[{**_HELD, "charge": 1}])
    with pytest.raises(BadInput, match="multiplicity 1 does not fit"):
        held.edit(id="held", ops=[{**_HELD, "multiplicity": 1}])
    with pytest.raises(BadInput, match="spin_polarization"):
        held.edit(id="held", ops=[{**_HELD, "spin_polarization": "yes"}])
    base = {k: v for k, v in _HELD.items() if k != "remove"}
    with pytest.raises(BadInput, match="only apply to a held shape"):
        held.edit(id="held", ops=[{**base, "multiplicity": 2}])


def test_fragment_and_cone_selectors_parse_canonicalise_and_refuse() -> None:
    from precis_se.datums import _selector_text, parse_selector
    from precis_se.measures import MeasureError

    sel = parse_selector("mpatch:nh3!aH3:2")
    assert (sel.instance, sel.without, sel.patch_id) == ("nh3", "aH3", 2)
    assert _selector_text(sel) == "mpatch:nh3!aH3:2"
    assert _selector_text(parse_selector("mol:nh3!aH3@0")) == "mol:nh3!aH3@0.0"
    cone = parse_selector("mcone:nh3!aH3:aN1@0,0,-1/30+3")
    assert (cone.anchor, cone.axis, cone.half_angle_deg) == ("aN1", (0, 0, -1), 30)
    assert cone.length == pytest.approx(3e-10)
    text = _selector_text(cone)
    assert text == "mcone:nh3!aH3:aN1@0.0,0.0,-1.0/30.0+3.0"
    assert parse_selector(text) == cone
    for bad, match in [
        ("mcone:nh3:aN1@0,0,0/30+3", "nonzero"),
        ("mcone:nh3:aN1@0,0,1/95+3", "half-angle"),
        ("mcone:nh3:aN1@0,0,1/30+0", "positive"),
        ("mcone:nh3@0,0,1/30+3", "mcone selector"),
        ("mpatch:nh3!:0", "removed atom"),
    ]:
        with pytest.raises(MeasureError, match=match):
            parse_selector(bad)


def test_bang_is_reserved_in_block_names(store: Store) -> None:
    se = SeHandler(hub=Hub(store=store))
    with pytest.raises(BadInput, match="must not contain"):
        se.put(
            id="bang",
            text=json.dumps({"ops": [{"op": "add_block", "name": "a!b"}]}),
        )


def test_real_gfn2_strain_of_nh2_frozen_at_nh3_is_positive(
    store: Store, taxonomy: Store
) -> None:
    pytest.importorskip("tblite")
    from precis_se.properties.pocket_spec import held_shape

    se = _design(store, "real", _wall_ring())
    shape = held_shape(_tree(se, "real"), "nh3", _REMOVED)
    assert shape.multiplicity == 2
    assert shape.strain_eV > 0.0
