"""``view='surface'`` — a posed molecule's surface painted with ESP, H-bond
and polarity classes (docs/backlog/pocket-from-molecule.md slice 1;
:mod:`precis_se.properties.surface`).

The painter is tested on fixed charges (no backend), the view through a
stub TBLite that records what it was handed, and the real GFN2 path only
where tblite is installed (PyPI has no linux-aarch64 wheel, so those
skip there, as in ``test_structure_relax_xtb.py``).
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import numpy as np
import pytest
from ase.calculators.calculator import Calculator, all_changes

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.structure.cell import Cell
from precis.structure.scene import Atom as StructAtom
from precis.structure.scene import Scene as StructScene
from precis_se.handler import SeHandler
from precis_se.properties.surface import (
    SURFACE_SETTINGS,
    classify_atoms,
    paint_surface,
    vdw_radii,
)

#: Water (O, H, H), Å, H–O–H 104.5°.
_WATER = np.array([[0.0, 0.0, 0.0], [0.757, 0.586, 0.0], [-0.757, 0.586, 0.0]])
#: GFN2 Mulliken charges of that geometry (tblite, 2026-10-17).
_WATER_Q = np.array([-0.564, 0.282, 0.282])
_NO = np.array([[0.0, 0.0, 0.0], [1.155, 0.0, 0.0]])


def _water_surface(scale: float = 1.0) -> Any:
    from dataclasses import replace

    return paint_surface(
        ["aO1", "aH1", "aH2"],
        ["O", "H", "H"],
        _WATER,
        _WATER_Q,
        [0, 0, 0],
        replace(SURFACE_SETTINGS, scale=scale),
    )


@pytest.mark.parametrize("scale", [1.0, 1.4])
def test_water_points_sit_on_their_scaled_vdw_sphere(scale: float) -> None:
    """Every point lies within 0.05 Å of scale × vdW radius from its
    nearest (owning) atom, and inside no other atom's sphere."""
    surf = _water_surface(scale)
    radii = vdw_radii(surf.elements, scale)
    assert len(surf.points) > 50
    own = np.linalg.norm(surf.points - _WATER[surf.owner], axis=1)
    assert np.abs(own - radii[surf.owner]).max() < 0.05
    dist = np.linalg.norm(surf.points[:, None, :] - _WATER[None, :, :], axis=2)
    assert (dist >= radii[None, :] - 1e-6).all()
    assert (
        np.einsum("ij,ij->i", surf.normals, surf.points - _WATER[surf.owner]) > 0
    ).all()


def test_water_classes_and_esp_signs() -> None:
    """Both H are donors pointing O→H, O is an acceptor pointing away from
    the H pair; the ESP is negative on O's lone-pair side and positive at
    each H tip, and every point carries its atom's classes."""
    surf = _water_surface()
    o, h1, h2 = surf.classes
    assert (h1.hbond, h2.hbond, o.hbond) == ("donor", "donor", "acceptor")
    assert all(c.polarity == "polar" for c in surf.classes)
    assert h1.direction is not None and o.direction is not None
    assert np.allclose(h1.direction, _WATER[1] / np.linalg.norm(_WATER[1]), atol=1e-6)
    assert np.allclose(o.direction, [0.0, -1.0, 0.0], atol=1e-6)
    for atom, cls in enumerate(surf.classes):
        mine = surf.owner == atom
        assert mine.any()
        assert cls.direction is not None
        tip = int(np.argmax(surf.normals[mine] @ cls.direction))
        esp_tip = float(surf.esp_V[mine][tip])
        assert esp_tip < 0 if atom == 0 else esp_tip > 0
    assert float(surf.esp_V[surf.owner == 0].mean()) < 0
    assert float(surf.esp_V[surf.owner != 0].mean()) > 0


def test_acceptor_rule_follows_lone_pairs() -> None:
    """NO: both atoms accept, pointing away from each other. NH4+ (formal
    +1 on N) has no lone pair, so no acceptor; its H still donate.
    Methane is nonpolar throughout."""
    no = classify_atoms(["N", "O"], _NO, [0, 0])
    assert [c.hbond for c in no] == ["acceptor", "acceptor"]
    assert no[0].direction is not None and no[0].direction[0] < -0.99
    tet = np.array([[1, 1, 1], [-1, -1, 1], [-1, 1, -1], [1, -1, -1]]) * 0.6
    ammonium = classify_atoms(
        ["N", "H", "H", "H", "H"], np.vstack([np.zeros(3), tet]), [1, 0, 0, 0, 0]
    )
    assert ammonium[0].hbond == "none" and ammonium[0].polarity == "polar"
    assert {c.hbond for c in ammonium[1:]} == {"donor"}
    methane = classify_atoms(
        ["C", "H", "H", "H", "H"], np.vstack([np.zeros(3), tet]), [0] * 5
    )
    assert {(c.hbond, c.polarity) for c in methane} == {("none", "nonpolar")}


# ── the se view ─────────────────────────────────────────────────────────


class _FakeTBLite(Calculator):
    """A TBLite stand-in: records its kwargs, returns ±0.06 e charges."""

    implemented_properties = ["energy", "free_energy", "forces", "charges"]
    seen: list[dict[str, Any]] = []

    def __init__(self, atoms=None, **kwargs: Any):
        super().__init__(atoms=atoms)
        _FakeTBLite.seen.append(kwargs)

    def calculate(
        self, atoms=None, properties=("energy",), system_changes=all_changes
    ) -> None:
        super().calculate(atoms, properties, system_changes)
        assert self.atoms is not None
        n = len(self.atoms)
        q = np.array([0.06 if i % 2 == 0 else -0.06 for i in range(n)])
        self.results = {
            "energy": -185.9,
            "free_energy": -185.9,
            "forces": np.zeros((n, 3)),
            "charges": q - q.mean(),
        }


def _stub_tblite(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("tblite.ase")
    setattr(fake, "TBLite", _FakeTBLite)  # a ModuleType has no TBLite attr
    monkeypatch.setitem(sys.modules, "tblite.ase", fake)
    monkeypatch.setattr(_FakeTBLite, "seen", [])


def _design(store: Any, slug: str, elements: list[str], xyz: np.ndarray) -> SeHandler:
    """A design ``slug`` whose block ``mol`` (posed at x = 1 nm) binds a
    non-periodic structure holding ``elements`` at ``xyz`` (Å)."""
    cell = Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False))
    scene = StructScene(cell=cell)
    for el, p in zip(elements, xyz, strict=True):
        label = scene.next_label(el)
        frac = cell.cart_to_frac(np.asarray(p) + 10.0)
        scene.atoms[label] = StructAtom(label, el, frac)
    store.structure_save(
        slug=f"{slug}-struct",
        title=slug,
        scene=scene,
        version=1,
        card_text="surface test molecule.",
    )
    se = SeHandler(hub=Hub(store=store))
    se.put(
        id=slug,
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "mol",
                        "envelope": "sphere:r4e-10",
                        "pose": "[1 0 0] nm",
                    },
                    {
                        "op": "bind_structure",
                        "block": "mol",
                        "design": f"{slug}-struct",
                        "ports": {},
                    },
                ]
            }
        ),
    )
    return se


def test_surface_view_records_no_as_a_doublet(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On NO the read records multiplicity 2 — by default (15 electrons,
    labelled a default) and when passed — and hands it to tblite; the
    JSON payload carries every point in the se world frame."""
    _stub_tblite(monkeypatch)
    se = _design(store, "no-surface", ["N", "O"], _NO)
    body = se.get(id="no-surface", view="surface", args={"block": "mol"}).body
    assert "multiplicity: 2 (default: lowest spin for 15 electrons" in body
    assert "spin population: not returned" in body
    payload = json.loads(
        se.get(
            id="no-surface",
            view="surface",
            args={"block": "mol", "multiplicity": 2, "format": "json"},
        ).body
    )
    assert payload["multiplicity"] == 2 and payload["charge"] == 0
    assert payload["multiplicity_source"] == "args.multiplicity"
    assert payload["electrons"] == 15 and payload["spin_population"] is None
    assert [kw["multiplicity"] for kw in _FakeTBLite.seen] == [2, 2]
    assert [a["hbond"] for a in payload["atoms"]] == ["acceptor", "acceptor"]
    pts = np.array([p["pos"] for p in payload["points"]])
    assert len(pts) > 50
    owner = np.array([p["atom"] for p in payload["points"]])
    # stored at the 20 Å box centre, then the block pose adds x = 1 nm
    centres = _NO + 10.0 + np.array([10.0, 0.0, 0.0])
    radii = vdw_radii(["N", "O"], SURFACE_SETTINGS.scale)
    off = np.linalg.norm(pts - centres[owner], axis=1) - radii[owner]
    assert np.abs(off).max() < 0.05


def test_surface_view_refuses_what_does_not_fit(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A singlet NO, an unknown args key, a missing block arg and a bad
    format are BadInput before any backend work."""
    _stub_tblite(monkeypatch)
    se = _design(store, "no-bad", ["N", "O"], _NO)
    with pytest.raises(BadInput, match="lowest is 2"):
        se.get(id="no-bad", view="surface", args={"block": "mol", "multiplicity": 1})
    with pytest.raises(BadInput, match="unknown args key"):
        se.get(id="no-bad", view="surface", args={"block": "mol", "spin": 1})
    with pytest.raises(BadInput, match="requires args"):
        se.get(id="no-bad", view="surface")
    with pytest.raises(BadInput, match="'text' or 'json'"):
        se.get(id="no-bad", view="surface", args={"block": "mol", "format": "xyz"})
    assert _FakeTBLite.seen == []
    assert "surface" in SeHandler.spec.views
    with pytest.raises(BadInput) as exc:
        se.get(id="no-bad", view="nope")
    assert "view='surface'" in str(exc.value.next)


def test_surface_view_unsupported_without_tblite(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With tblite absent the view is Unsupported, not a traceback."""
    se = _design(store, "water-no-tblite", ["O", "H", "H"], _WATER)
    monkeypatch.setitem(sys.modules, "tblite.ase", None)
    with pytest.raises(Unsupported, match="GFN2-xTB") as exc:
        se.get(id="water-no-tblite", view="surface", args={"block": "mol"})
    assert "not dispatched" in str(exc.value.next)


def test_surface_view_real_gfn2_water_and_no(store: Any) -> None:
    """Real GFN2: water's O carries acceptor points with negative mean ESP
    and the H donor points a positive one; NO reads as a doublet with no
    spin population returned."""
    pytest.importorskip("tblite")
    se = _design(store, "water-real", ["O", "H", "H"], _WATER)
    water = json.loads(
        se.get(
            id="water-real", view="surface", args={"block": "mol", "format": "json"}
        ).body
    )
    q = [a["charge_e"] for a in water["atoms"]]
    assert q[0] < -0.3 and q[1] > 0.1 and q[2] > 0.1
    by_class: dict[str, list[float]] = {}
    for p in water["points"]:
        by_class.setdefault(p["hbond"], []).append(p["esp_V"])
    assert set(by_class) == {"acceptor", "donor"}
    assert np.mean(by_class["acceptor"]) < 0 < np.mean(by_class["donor"])
    se_no = _design(store, "no-real", ["N", "O"], _NO)
    no = json.loads(
        se_no.get(
            id="no-real", view="surface", args={"block": "mol", "format": "json"}
        ).body
    )
    assert no["multiplicity"] == 2 and no["spin_population"] is None
    assert abs(sum(a["charge_e"] for a in no["atoms"])) < 1e-6
