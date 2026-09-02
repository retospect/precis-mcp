"""Tests for the MLIP preflight gate (:mod:`precis.structure.preflight`).

Pure-python Scene fixtures (no DB) — built the same way ``ops.apply_ops``
tests do: a ``slab`` op for a real fcc(111) metal surface, plus direct
``Atom`` inserts for the pathological cases. ASE is required for anything
past the element-in-box check, so every test past the first gates on it.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis.structure import ops
from precis.structure.cell import Cell
from precis.structure.scene import FIX_ALL, Atom, Scene

pytest.importorskip("ase.build")

from precis.structure import preflight as pf


def _pd_slab(size=(2, 2, 3), vacuum: float = 10.0, fix_layers: int = 1) -> Scene:
    scene = Scene(cell=Cell(np.eye(3)))
    ops.apply_ops(
        scene,
        [
            {
                "op": "slab",
                "element": "Pd",
                "size": list(size),
                "vacuum": vacuum,
                "fix_layers": fix_layers,
            }
        ],
    )
    return scene


class TestElementInBox:
    def test_out_of_box_element_flagged_without_relax(self) -> None:
        scene = _pd_slab()
        label = scene.next_label("Xe")
        scene.atoms[label] = Atom(
            label=label, element="Xe", frac=np.array([0.5, 0.5, 0.6])
        )
        verdict = pf.preflight(scene)
        reasons = [r for r in verdict.reasons if r.code == "element_out_of_box"]
        assert reasons and reasons[0].atom == label
        assert reasons[0].element == "Xe"
        assert verdict.ok is False

    def test_backend_elements_override_widens_the_box(self) -> None:
        scene = _pd_slab()
        label = scene.next_label("Xe")
        scene.atoms[label] = Atom(
            label=label, element="Xe", frac=np.array([0.5, 0.5, 0.6])
        )
        verdict = pf.preflight(scene, backend_elements={"Pd", "Xe"})
        assert not any(r.code == "element_out_of_box" for r in verdict.reasons)


class TestCleanSlab:
    def test_clean_relaxed_slab_is_ok(self) -> None:
        scene = _pd_slab()
        verdict = pf.preflight(scene)
        assert verdict.ok is True
        assert verdict.reasons == []


class TestCeiling:
    def test_atom_high_in_vacuum_flagged_as_ceiling(self) -> None:
        scene = _pd_slab()
        label = "aPdHigh"
        scene.atoms[label] = Atom(
            label=label, element="Pd", frac=np.array([0.5, 0.5, 0.99])
        )
        verdict = pf.preflight(scene)
        ceiling = [r for r in verdict.reasons if r.code == "ceiling"]
        assert any(r.atom == label for r in ceiling)
        assert verdict.ok is False


class TestDetached:
    def test_untethered_adsorbate_flagged_as_detached(self) -> None:
        scene = _pd_slab()
        cart = np.array([scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()])
        top_z = float(cart[:, 2].max())
        center_xy = cart[:, :2].mean(axis=0)
        placement = np.array([center_xy[0], center_xy[1], top_z + 5.0])
        frac = scene.cell.wrap(scene.cell.cart_to_frac(placement))
        label = scene.next_label("O")
        scene.atoms[label] = Atom(label=label, element="O", frac=frac)

        verdict = pf.preflight(scene)
        detached = [r for r in verdict.reasons if r.code == "detached"]
        assert any(r.atom == label for r in detached)
        assert verdict.ok is False


class TestClash:
    def test_coincident_atoms_cannot_settle_apart(self) -> None:
        scene = _pd_slab()
        frac = np.array([0.2, 0.2, 0.55])
        labels = []
        for _ in range(2):
            label = scene.next_label("C")
            scene.atoms[label] = Atom(label=label, element="C", frac=frac.copy())
            labels.append(label)

        verdict = pf.preflight(scene)
        clashes = [r for r in verdict.reasons if r.code == "clash"]
        assert any({r.atom} & set(labels) for r in clashes)
        assert verdict.ok is False


class TestPorous:
    def test_sparse_arrangement_flagged_as_porous(self) -> None:
        scene = Scene(cell=Cell(np.diag([20.0, 20.0, 20.0]), (True, True, True)))
        positions = [
            [0.1, 0.1, 0.1],
            [0.5, 0.5, 0.5],
            [0.9, 0.1, 0.3],
            [0.3, 0.8, 0.7],
        ]
        for i, frac in enumerate(positions):
            label = f"aCu{i + 1}"
            scene.atoms[label] = Atom(label=label, element="Cu", frac=np.array(frac))

        verdict = pf.preflight(scene)
        assert any(r.code == "porous" for r in verdict.reasons)
        assert verdict.ok is False


class TestElementAgnostic:
    def test_non_emt_metal_runs_all_checks_without_crashing(self) -> None:
        """Fe isn't in :data:`relax.EMT_ELEMENTS` — proves the settle/geometry
        checks don't secretly depend on EMT's closed palette."""
        cell = Cell.from_lengths_angles(8.4, 8.4, 24.0, pbc=(True, True, False))
        scene = Scene(cell=cell)
        layer_z = 0.3
        for i, (x, y) in enumerate([(0.0, 0.0), (0.5, 0.0), (0.0, 0.5), (0.5, 0.5)]):
            label = f"aFe{i + 1}"
            scene.atoms[label] = Atom(
                label=label,
                element="Fe",
                frac=np.array([x, y, layer_z]),
                fixed=FIX_ALL if i < 2 else 0,
            )
        scene.atoms["aN1"] = Atom(
            label="aN1", element="N", frac=np.array([0.25, 0.25, layer_z + 0.05])
        )

        verdict = pf.preflight(scene)
        assert isinstance(verdict, pf.PreflightVerdict)
        assert isinstance(verdict.ok, bool)
        assert all(isinstance(r, pf.PreflightReason) for r in verdict.reasons)


class TestDomainCaveats:
    """gripe 285774: advisory-only chemistry-domain signals a plain element
    check can't catch — a metal-organic straddle, a declared net charge.

    The pure unit-level checks go straight at ``_domain_checks`` (no ASE/
    settle involved — it's a composition/oxidation scan) so they're immune
    to the unrelated vacuum/density thresholds the geometry checks apply
    (:class:`TestPorous` shows a sparse arrangement alone can flag
    'porous' — irrelevant noise for a caveat-only unit test). One
    full-pipeline test below proves the caveat also surfaces through
    :func:`pf.preflight` without turning a clean geometry's ``ok`` False."""

    def _molecule(self, atoms: list[tuple[str, int | None]]) -> Scene:
        """A small non-periodic molecule scene: ``[(element, oxidation), ...]``."""
        scene = Scene(cell=Cell.from_lengths_angles(20.0, 20.0, 20.0, pbc=(False,) * 3))
        for i, (element, oxidation) in enumerate(atoms):
            label = f"a{element}{i + 1}"
            scene.atoms[label] = Atom(
                label=label,
                element=element,
                frac=np.array([0.1 * i, 0.5, 0.5]),
                oxidation=oxidation,
            )
        return scene

    def test_all_organic_neutral_scene_has_no_domain_caveat(self) -> None:
        scene = self._molecule([("C", None), ("H", None), ("O", None)])
        assert pf._domain_checks(scene) == []

    def test_metal_organic_straddle_is_an_advisory_caveat(self) -> None:
        """A Pd-organic mix — element-in-box passes (Pd and C/H are both in
        MACE-MP's palette), but the combination straddles both MLIPs'
        training domains; that must show up as a caveat, never a rejection."""
        scene = self._molecule([("Pd", None), ("C", None), ("H", None)])
        straddle = [c for c in pf._domain_checks(scene) if c.code == "domain_straddle"]
        assert straddle and "Pd" in straddle[0].message

    def test_net_charge_is_an_advisory_caveat(self) -> None:
        scene = self._molecule([("N", 1), ("H", None), ("H", None)])
        charged = [c for c in pf._domain_checks(scene) if c.code == "domain_charged"]
        assert charged and "+1" in charged[0].message

    def test_balanced_net_charge_is_not_flagged(self) -> None:
        """Individually charged sites that sum to a formally neutral
        molecule (a zwitterion, e.g.) shouldn't trip the net-charge caveat."""
        scene = self._molecule([("N", 1), ("O", -1)])
        assert not any(c.code == "domain_charged" for c in pf._domain_checks(scene))

    def test_straddle_caveat_surfaces_through_the_full_pipeline_without_gating(
        self,
    ) -> None:
        """A tethered (not detached) organic adsorbate on the Pd slab: the
        geometry checks all pass (:class:`TestCleanSlab`'s baseline), but the
        Pd/C mix still earns a ``domain_straddle`` caveat — proving caveats
        ride alongside a passing verdict rather than needing a failure to
        show up."""
        scene = _pd_slab()
        cart = np.array([scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()])
        top_z = float(cart[:, 2].max())
        center_xy = cart[:, :2].mean(axis=0)
        placement = np.array([center_xy[0], center_xy[1], top_z + 2.0])
        frac = scene.cell.wrap(scene.cell.cart_to_frac(placement))
        label = scene.next_label("C")
        scene.atoms[label] = Atom(label=label, element="C", frac=frac)

        verdict = pf.preflight(scene)
        assert any(c.code == "domain_straddle" for c in verdict.caveats)
        assert verdict.ok is True  # advisory caveat never blocks a clean geometry
