"""Wiring tests for the Tier-0 structure preflight gate (:mod:`precis.structure
.preflight`) into its handler seam: the structure handler's ``put``/``edit`` —
a hard reject + undo (nothing persists on a failing verdict), gated behind
``PRECIS_STRUCTURE_PREFLIGHT`` (default off). The other seam
(``quest.compute.dispatch_autocatpath``) has its own wiring tests in
``test_quest_compute.py``.

The base fixture is a real fcc(111) Pd slab (the ``slab`` bulk template, same
op params as ``test_structure_preflight.py``'s ``TestCleanSlab``) — a clean
settled slab preflight already passes, needed here because the flag-ON tests
put/edit it *while the gate is live*, so the fixture itself must clear the
gate before any test-specific op is layered on.
"""

from __future__ import annotations

import json

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import structure as structure_mod
from precis.handlers.structure import StructureHandler
from precis.structure import preflight as preflight_mod

pytest.importorskip("ase.build")

_SLAB_OP = {
    "op": "slab",
    "element": "Pd",
    "size": [2, 2, 3],
    "vacuum": 10.0,
    "fix_layers": 1,
}
_PD_SLAB = json.dumps({"ops": [_SLAB_OP]})

# He is a noble gas — outside MACE_MP_ELEMENTS (element_out_of_box), so this
# op fails the gate deterministically.
_BAD_ELEMENT_OP = {"op": "add_atom", "element": "He", "frac": [0.5, 0.5, 0.5]}

# A pure marker (no geometry change at all) — a guaranteed-clean edit against
# an already-clean slab.
_CLEAN_OP = {"op": "eye", "name": "active_site", "atoms": ["aPd1"]}

_ENV = preflight_mod._PREFLIGHT_ENABLED_ENV


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


class TestEditPreflightGate:
    def test_flag_off_is_a_no_op_gate(self, structure, monkeypatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        structure.put(id="pd_slab", text=_PD_SLAB)
        resp = structure.edit(id="pd_slab", ops=[_BAD_ELEMENT_OP])
        assert "edited" in resp.body  # no rejection when the flag is off

    def test_flag_on_rejects_and_undoes(self, structure, monkeypatch) -> None:
        monkeypatch.setenv(_ENV, "1")
        structure.put(id="pd_slab", text=_PD_SLAB)
        ref = structure.store.get_ref(kind="structure", id="pd_slab")
        before_version = structure.store.structure_version(ref.id)

        with pytest.raises(BadInput, match="structure preflight rejected this edit"):
            structure.edit(id="pd_slab", ops=[_BAD_ELEMENT_OP])

        # nothing persisted — same version, the bad atom never landed
        assert structure.store.structure_version(ref.id) == before_version
        scene, _ = structure.store.structure_load(ref.id)
        assert not any(a.element == "He" for a in scene.atoms.values())

    def test_flag_on_still_allows_a_clean_edit(self, structure, monkeypatch) -> None:
        monkeypatch.setenv(_ENV, "1")
        structure.put(id="pd_slab", text=_PD_SLAB)
        resp = structure.edit(id="pd_slab", ops=[_CLEAN_OP])
        assert "edited" in resp.body

    def test_flag_on_surfaces_a_domain_caveat_without_blocking(
        self, structure, monkeypatch
    ) -> None:
        """gripe 285774: a passing verdict can still carry an advisory
        domain caveat (a metal-organic straddle) — surfaced in the response
        body as an echo, never turned into a rejection."""
        import numpy as np

        monkeypatch.setenv(_ENV, "1")
        structure.put(id="pd_slab_caveat", text=_PD_SLAB)
        ref = structure.store.get_ref(kind="structure", id="pd_slab_caveat")
        scene, _ = structure.store.structure_load(ref.id)
        cart = np.array([scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()])
        top_z = float(cart[:, 2].max())
        center_xy = cart[:, :2].mean(axis=0)
        placement = np.array([center_xy[0], center_xy[1], top_z + 2.0])
        frac = scene.cell.wrap(scene.cell.cart_to_frac(placement)).tolist()

        resp = structure.edit(
            id="pd_slab_caveat",
            ops=[{"op": "add_atom", "element": "C", "frac": frac}],
        )
        assert "edited" in resp.body  # advisory only — the edit still lands
        assert "preflight caveat" in resp.body
        assert "domain_straddle" not in resp.body  # message text, not the code
        assert "metal-organic" in resp.body or "off-distribution" in resp.body

    def test_fail_open_on_preflight_infra_error(self, structure, monkeypatch) -> None:
        """A preflight-internal error (e.g. ASE/[dft] missing) must not block
        the edit — fail open, only a real ``not ok`` verdict fails closed."""
        monkeypatch.setenv(_ENV, "1")
        structure.put(id="pd_slab", text=_PD_SLAB)

        def _boom(scene, **kwargs):
            raise ImportError("no ase (simulated)")

        monkeypatch.setattr(structure_mod, "_mlip_preflight", _boom)
        resp = structure.edit(id="pd_slab", ops=[_BAD_ELEMENT_OP])
        assert "edited" in resp.body


class TestPutPreflightGate:
    def test_flag_off_is_a_no_op_gate(self, structure, monkeypatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        payload = json.dumps({"ops": [_SLAB_OP, _BAD_ELEMENT_OP]})
        resp = structure.put(id="bad_slab", text=payload)
        assert "created" in resp.body

    def test_flag_on_rejects_bad_scene_and_creates_nothing(
        self, structure, monkeypatch
    ) -> None:
        monkeypatch.setenv(_ENV, "1")
        payload = json.dumps({"ops": [_SLAB_OP, _BAD_ELEMENT_OP]})
        with pytest.raises(BadInput, match="structure preflight rejected this edit"):
            structure.put(id="bad_slab", text=payload)
        assert structure.store.get_ref(kind="structure", id="bad_slab") is None

    def test_flag_on_still_allows_a_clean_put(self, structure, monkeypatch) -> None:
        monkeypatch.setenv(_ENV, "1")
        resp = structure.put(id="pd_slab", text=_PD_SLAB)
        assert "created" in resp.body
