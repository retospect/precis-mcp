"""Anchor-free site placement and slab-op refusals (gr547306, gr547673).

qu509651's Cu(111) dopant proposals kept dying on a ``slab`` op missing
fields it was never told about, and on hand-placed adatoms that clashed.
``add_atom_site``/``add_adsorbate`` now pick a free top/bridge/fcc/hcp
site themselves when ``anchors`` is omitted, ``set_element`` substitutes
by lattice site, and every such refusal carries a working ``next=``.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis.structure import OpError, Scene, apply_ops, validate
from precis.structure.cell import Cell

pytest.importorskip("ase.build")

_SLAB = {"op": "slab", "element": "Cu", "size": [3, 3, 4], "fix_layers": 2}


def _built(*ops: dict) -> Scene:
    scene = Scene(cell=Cell(np.eye(3)))
    apply_ops(scene, [_SLAB, *ops])
    return scene


def _cart(scene: Scene, label: str) -> np.ndarray:
    return scene.cell.frac_to_cart(scene.atoms[label].frac)


def _layer(scene: Scene, first: int, n: int = 9) -> list[str]:
    """Labels ``aCu<first>…`` of one 3×3 slab layer (ASE order, bottom-up)."""
    return [f"aCu{i}" for i in range(first, first + n)]


def _over_second_layer(scene: Scene, label: str) -> bool:
    """True when an atom of the second layer (aCu19…27) sits under ``label``
    in-plane — the hcp hollow."""
    xy = _cart(scene, label)[:2]
    lat = scene.cell.lattice
    for la in _layer(scene, 19):
        p = _cart(scene, la)
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                if np.linalg.norm((p + i * lat[0] + j * lat[1])[:2] - xy) < 0.5:
                    return True
    return False


def _errors(scene: Scene) -> list:
    return [f for f in validate(scene) if f.severity == "error"]


class TestAutoSite:
    @pytest.mark.parametrize(("kind", "over_atom"), [("fcc", False), ("hcp", True)])
    def test_fcc_and_hcp_land_on_the_named_hollow(
        self, kind: str, over_atom: bool
    ) -> None:
        scene = _built({"op": "add_atom_site", "element": "Ni", "site": {"type": kind}})
        assert _over_second_layer(scene, "aNi1") is over_atom
        top_z = max(_cart(scene, la)[2] for la in _layer(scene, 28))
        assert _cart(scene, "aNi1")[2] > top_z + 1.5
        assert _errors(scene) == []

    def test_dopant_adatom_o_ch4_needs_no_labels_and_passes_preflight(self) -> None:
        """The quest's Cu(111) bridging-O + CH4 + dopant-adatom build, with
        no anchor label or coordinate chosen by the caller."""
        from precis.structure import preflight as pf

        scene = _built(
            {"op": "add_atom_site", "element": "Rh", "site": {"type": "fcc"}},
            {
                "op": "add_adsorbate",
                "species": "O",
                "site": {"type": "bridge", "near": "aRh1"},
            },
            {
                "op": "add_adsorbate",
                "species": "CH4",
                "site": {"type": "top", "near": "aO1"},
                "height": 2.5,
            },
        )
        assert _errors(scene) == []
        verdict = pf.preflight(scene)
        assert verdict.ok is True, verdict.reasons
        # Each placement sits beside the atom it was asked to be near.
        d_o_rh = scene.cell.mic(scene.atoms["aO1"].frac, scene.atoms["aRh1"].frac)[0]
        d_c_o = scene.cell.mic(scene.atoms["aC1"].frac, scene.atoms["aO1"].frac)[0]
        assert d_o_rh < 3.5
        assert d_c_o < 3.0

    def test_occupied_site_is_skipped(self) -> None:
        """Two fcc adatoms in a row never land on the same hollow."""
        op = {"op": "add_atom_site", "element": "Ni", "site": {"type": "fcc"}}
        scene = _built(op, dict(op))
        d = scene.cell.mic(scene.atoms["aNi1"].frac, scene.atoms["aNi2"].frac)[0]
        assert d > 2.0
        assert _errors(scene) == []

    def test_pick_is_deterministic(self) -> None:
        op = {"op": "add_atom_site", "element": "Pt", "site": {"type": "hcp"}}
        a, b = _built(dict(op)), _built(dict(op))
        assert np.allclose(a.atoms["aPt1"].frac, b.atoms["aPt1"].frac)

    def test_anchors_over_the_other_hollow_refuse_naming_it(self) -> None:
        scene = _built()
        # aCu28/29/31 form an fcc hollow on the ASE fcc(111) [3,3,4].
        apply_ops(
            scene,
            [
                {
                    "op": "add_atom_site",
                    "element": "H",
                    "site": {"type": "fcc", "anchors": ["aCu28", "aCu29", "aCu31"]},
                }
            ],
        )
        assert _over_second_layer(scene, "aH1") is False
        with pytest.raises(OpError, match="form an fcc hollow, not hcp") as exc:
            apply_ops(
                scene,
                [
                    {
                        "op": "add_atom_site",
                        "element": "H",
                        "site": {
                            "type": "hcp",
                            "anchors": ["aCu28", "aCu29", "aCu31"],
                        },
                    }
                ],
            )
        assert str(exc.value).startswith('valid form: {"op":"add_atom_site"')
        assert exc.value.next

    @pytest.mark.parametrize("typo", ["anchor", "labels"])
    def test_misspelt_anchors_refuse_instead_of_auto_picking(self, typo: str) -> None:
        """Review fix: a typo'd key must not fall through to an auto-picked
        site somewhere the caller never asked for."""
        with pytest.raises(OpError, match="did you mean 'anchors'") as exc:
            _built(
                {
                    "op": "add_atom_site",
                    "element": "Ni",
                    "site": {"type": "top", typo: ["aCu28"]},
                }
            )
        assert f"unknown site key(s) ['{typo}']" in str(exc.value)
        assert "['type', 'anchors', 'near']" in str(exc.value)

    def test_null_anchors_keep_the_list_refusal(self) -> None:
        with pytest.raises(OpError, match="must be a list of atom labels"):
            _built(
                {
                    "op": "add_atom_site",
                    "element": "Ni",
                    "site": {"type": "top", "anchors": None},
                }
            )

    def test_anchors_with_near_refuse(self) -> None:
        with pytest.raises(OpError, match="'anchors' .* or 'near' .*not both"):
            _built(
                {
                    "op": "add_adsorbate",
                    "species": "O",
                    "site": {"type": "top", "anchors": ["aCu28"], "near": "aCu29"},
                }
            )

    def test_unknown_near_names_the_roster(self) -> None:
        with pytest.raises(OpError, match="'near' 'aZz9'") as exc:
            _built(
                {
                    "op": "add_atom_site",
                    "element": "Ni",
                    "site": {"type": "top", "near": "aZz9"},
                }
            )
        assert exc.value.next

    def test_no_slab_refuses_with_a_next(self) -> None:
        scene = Scene(cell=Cell(np.eye(3) * 10.0))
        with pytest.raises(OpError, match="needs a metal slab") as exc:
            apply_ops(
                scene,
                [{"op": "add_atom_site", "element": "H", "site": {"type": "top"}}],
            )
        assert str(exc.value).startswith('valid form: {"op":"add_atom_site"')

    def test_long_got_repr_keeps_the_example_under_the_note_cap(self) -> None:
        """Review fix: the working op leads the error, so a quest's capped
        rejected-proposal note keeps it however long the bad value runs."""
        from precis.handlers.structure import _op_bad_input
        from precis.quest.compute import _REJECT_REASON_CAP, _reject_reason

        labels = [f"aCu{i}" for i in range(1, 37)] * 4
        with pytest.raises(OpError) as exc:
            _built(
                {
                    "op": "add_atom_site",
                    "element": "Ni",
                    "site": {"type": "top", "anchors": labels},
                }
            )
        note = _reject_reason(_op_bad_input(exc.value))
        assert len(str(_op_bad_input(exc.value))) > _REJECT_REASON_CAP
        assert '{"op":"add_atom_site","element":"Ni","site":{"type":"fcc"}}' in note


class TestSubstitutionBySite:
    def test_top_and_subsurface_pick_host_atoms_of_that_layer(self) -> None:
        scene = _built(
            {"op": "set_element", "element": "Ni", "site": {"type": "top"}},
            {"op": "set_element", "element": "Ni", "site": {"type": "top"}},
            {
                "op": "set_element",
                "element": "Pd",
                "site": {"type": "subsurface", "near": "Ni"},
            },
        )
        doped = {la: a.element for la, a in scene.atoms.items() if a.element != "Cu"}
        ni = [la for la, el in doped.items() if el == "Ni"]
        pd = [la for la, el in doped.items() if el == "Pd"]
        # Two distinct top-layer atoms (the second pick skips the dopant),
        # one second-layer atom; labels are kept.
        assert len(ni) == 2 and set(ni) <= set(_layer(scene, 28))
        assert len(pd) == 1 and set(pd) <= set(_layer(scene, 19))
        assert len(scene.atoms) == 36

    def test_bad_site_refuses_with_both_forms(self) -> None:
        with pytest.raises(OpError, match="'atom'") as exc:
            _built({"op": "set_element", "element": "Ni", "site": {"type": "fcc"}})
        assert '"site":{"type":"top"}' in str(exc.value)
        assert exc.value.next and '"atom":"aCu28"' in exc.value.next

    def test_atom_and_site_together_refuse(self) -> None:
        """Review fix: both given is an error, not 'atom wins'."""
        with pytest.raises(OpError, match="'atom' .* or 'site' .*not both"):
            _built(
                {
                    "op": "set_element",
                    "element": "Ni",
                    "atom": "aCu28",
                    "site": {"type": "top"},
                }
            )

    def test_site_with_anchors_refuses_naming_the_valid_keys(self) -> None:
        with pytest.raises(OpError, match=r"a site reads only \['type', 'near'\]"):
            _built(
                {
                    "op": "set_element",
                    "element": "Ni",
                    "site": {"type": "top", "anchors": ["aCu28"]},
                }
            )


class TestSlabRefusals:
    def test_missing_fields_are_named_with_a_valid_example(self) -> None:
        scene = Scene(cell=Cell(np.eye(3)))
        with pytest.raises(OpError) as exc:
            apply_ops(scene, [{"op": "slab", "metal": "Cu", "layers": 4}])
        msg = str(exc.value)
        assert "'element' (host metal symbol; absent)" in msg
        assert "'size' ([nx, ny, nz]; absent)" in msg
        assert "ignored key(s) ['layers', 'metal']" in msg
        assert msg.startswith("valid form: {")
        assert exc.value.next and "precis-structure-slab-help" in exc.value.next
        # The leading example is a working op: copy it and the slab builds.
        import json

        op, _end = json.JSONDecoder().raw_decode(msg.removeprefix("valid form: "))
        apply_ops(scene, [op])
        assert len(scene.atoms) == 36

    def test_malformed_size_reports_what_it_got(self) -> None:
        scene = Scene(cell=Cell(np.eye(3)))
        with pytest.raises(OpError, match=r"'size' \(\[nx, ny, nz\]; got \[3, 3\]\)"):
            apply_ops(scene, [{"op": "slab", "element": "Cu", "size": [3, 3]}])

    def test_long_size_list_keeps_the_example_under_the_note_cap(self) -> None:
        """Review fix: a long ``got`` repr pushes the cause past the quest's
        rejected-proposal cap; the example leads, so it survives."""
        from precis.handlers.structure import _op_bad_input
        from precis.quest.compute import _REJECT_REASON_CAP, _reject_reason

        scene = Scene(cell=Cell(np.eye(3)))
        with pytest.raises(OpError) as exc:
            apply_ops(scene, [{"op": "slab", "element": "Cu", "size": [3] * 200}])
        bad = _op_bad_input(exc.value)
        assert len(str(bad)) > _REJECT_REASON_CAP
        assert '"size":[3,3,4]' in _reject_reason(bad)

    def test_non_fcc_metal_without_a_is_a_clean_refusal(self) -> None:
        scene = Scene(cell=Cell(np.eye(3)))
        with pytest.raises(OpError, match="needs 'a'") as exc:
            apply_ops(scene, [{"op": "slab", "element": "Ru", "size": [3, 3, 4]}])
        assert exc.value.next
