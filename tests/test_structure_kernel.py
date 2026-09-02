"""Unit tests for the pure ``structure`` IR kernel (increment 1).

DB-free: exercises cell/MIC, ops, probes, and the validator gate directly. The
store + handler (DB layer) are covered separately once they land.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis.structure import (
    RelaxUnsupported,
    Scene,
    apply_ops,
    export,
    probe,
    relax,
    validate,
)
from precis.structure.cell import Cell
from precis.structure.relax import route_ml_model


def _cubic(a: float = 10.0, pbc: tuple[bool, bool, bool] = (True, True, True)) -> Cell:
    return Cell.from_lengths_angles(a, a, a, pbc=pbc)


# -- cell / MIC --------------------------------------------------------------


def test_frac_cart_roundtrip() -> None:
    cell = _cubic(3.0)
    assert np.allclose(cell.frac_to_cart(np.array([0.5, 0.0, 0.0])), [1.5, 0.0, 0.0])
    f = np.array([0.2, 0.7, 0.4])
    assert np.allclose(cell.cart_to_frac(cell.frac_to_cart(f)), f)
    assert cell.volume == pytest.approx(27.0)


def test_mic_picks_nearest_image_and_offset() -> None:
    cell = _cubic(10.0)
    dist, img = cell.mic(np.array([0.1, 0.0, 0.0]), np.array([0.9, 0.0, 0.0]))
    assert dist == pytest.approx(2.0)  # across the wall, not 8 Å in-cell
    assert img == (-1, 0, 0)


def test_mic_no_pbc_is_direct() -> None:
    cell = _cubic(10.0, pbc=(False, False, False))
    dist, img = cell.mic(np.array([0.1, 0.0, 0.0]), np.array([0.9, 0.0, 0.0]))
    assert dist == pytest.approx(8.0)
    assert img == (0, 0, 0)


def test_wrap_outside_box_comes_inside() -> None:
    cell = _cubic(10.0)
    assert np.allclose(cell.wrap(np.array([1.1, -0.2, 0.5])), [0.1, 0.8, 0.5])


# -- ops ---------------------------------------------------------------------


def test_add_atom_mints_labels_and_wraps() -> None:
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [1.26, 0.0, 0.0]},  # >1 wraps
        ],
    )
    assert set(scene.atoms) == {"aPd1", "aPd2"}
    assert scene.atoms["aPd2"].frac[0] == pytest.approx(0.26)


def test_set_element_vacancy_and_label_no_recycle() -> None:
    scene = Scene(cell=_cubic())
    apply_ops(scene, [{"op": "add_atom", "element": "Pd", "frac": [0, 0, 0]}])
    apply_ops(scene, [{"op": "add_atom", "element": "Pd", "frac": [0.3, 0, 0]}])
    apply_ops(scene, [{"op": "set_element", "atom": "aPd1", "element": "Cu"}])
    assert scene.atoms["aPd1"].element == "Cu"
    apply_ops(scene, [{"op": "vacancy", "atom": "aPd2"}])
    assert "aPd2" not in scene.atoms
    # next Pd label keeps climbing from the live max (aPd1 is now Cu, so 1)
    assert scene.next_label("Pd") == "aPd2"


def test_set_element_phantom_relabel_hints_stable_label() -> None:
    # The strong-model trap: set_element KEEPS the label, so an aPd1 doped to Cu
    # stays aPd1. A model that then references the phantom aCu1 should get a
    # message that names the label-retention rule + points at the real atom.
    from precis.structure import OpError

    scene = Scene(cell=_cubic())
    apply_ops(scene, [{"op": "add_atom", "element": "Pd", "frac": [0, 0, 0]}])
    apply_ops(scene, [{"op": "set_element", "atom": "aPd1", "element": "Cu"}])
    with pytest.raises(OpError) as exc:
        apply_ops(scene, [{"op": "set_element", "atom": "aCu1", "element": "Ni"}])
    msg = str(exc.value)
    assert "aPd1" in msg  # points at the atom the caller meant
    assert "stable" in msg.lower() or "keeps" in msg.lower()


def test_bad_ref_message_rosters_when_no_position_match() -> None:
    # A label with no matching position falls back to a roster of what exists.
    from precis.structure import OpError

    scene = Scene(cell=_cubic())
    apply_ops(scene, [{"op": "add_atom", "element": "Pd", "frac": [0, 0, 0]}])
    with pytest.raises(OpError) as exc:
        apply_ops(scene, [{"op": "vacancy", "atom": "aXx"}])
    assert "aPd1" in str(exc.value)


def test_bonds_add_remove_and_constrain() -> None:
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "C", "frac": [0.15, 0.0, 0.0]},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 2},
            {"op": "constrain", "atoms": ["aC1"], "kind": "fixed-all"},
        ],
    )
    assert len(scene.bonds) == 1 and scene.bonds[0].order == 2.0
    assert scene.atoms["aC1"].fixed == 7  # FIX_ALL
    apply_ops(scene, [{"op": "remove_bond", "i": "aC2", "j": "aC1"}])
    assert scene.bonds == []


# -- add_atom_site (site-symbolic placement) ---------------------------------


def _pd_triangle() -> Scene:
    """Three Pd anchors in a flat triangle (a toy hollow/top/bridge site)."""
    scene = Scene(cell=_cubic(20.0))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "cart": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "cart": [2.75, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "cart": [1.375, 2.38, 0.0]},
        ],
    )
    return scene


def test_add_atom_site_hollow_resolves_to_anchor_centroid() -> None:
    pytest.importorskip("ase")
    from ase.data import atomic_numbers, covalent_radii

    scene = _pd_triangle()
    apply_ops(
        scene,
        [
            {
                "op": "add_atom_site",
                "element": "H",
                "site": {
                    "type": "hollow",
                    "anchors": ["aPd1", "aPd2", "aPd3"],
                },
            }
        ],
    )
    labels = [
        la for la in scene.atoms if la != "aPd1" and la != "aPd2" and la != "aPd3"
    ]
    assert len(labels) == 1
    h = scene.atoms[labels[0]]
    assert h.element == "H"
    cart = scene.cell.frac_to_cart(h.frac)
    # xy = centroid of the three anchors
    assert cart[0] == pytest.approx((0.0 + 2.75 + 1.375) / 3, abs=1e-6)
    assert cart[1] == pytest.approx((0.0 + 0.0 + 2.38) / 3, abs=1e-6)
    # z = anchor plane (0.0) + covalent-radius-sum default height
    expected_h = (
        covalent_radii[atomic_numbers["Pd"]] + covalent_radii[atomic_numbers["H"]]
    )
    assert cart[2] == pytest.approx(expected_h, abs=1e-6)


def test_add_atom_site_top_resolves_to_single_anchor_xy() -> None:
    scene = _pd_triangle()
    apply_ops(
        scene,
        [
            {
                "op": "add_atom_site",
                "element": "H",
                "site": {"type": "top", "anchors": ["aPd1"]},
                "height": 1.5,
            }
        ],
    )
    labels = [
        la for la in scene.atoms if la != "aPd1" and la != "aPd2" and la != "aPd3"
    ]
    h = scene.atoms[labels[0]]
    cart = scene.cell.frac_to_cart(h.frac)
    assert cart[0] == pytest.approx(0.0, abs=1e-6)
    assert cart[1] == pytest.approx(0.0, abs=1e-6)
    assert cart[2] == pytest.approx(1.5, abs=1e-6)


def test_add_atom_site_unknown_anchor_names_the_bad_label() -> None:
    from precis.structure import OpError

    scene = _pd_triangle()
    with pytest.raises(OpError) as exc:
        apply_ops(
            scene,
            [
                {
                    "op": "add_atom_site",
                    "element": "H",
                    "site": {"type": "top", "anchors": ["aPd99"]},
                }
            ],
        )
    assert "aPd99" in str(exc.value)


def test_add_atom_site_wrong_anchor_count_raises() -> None:
    from precis.structure import OpError

    scene = _pd_triangle()
    with pytest.raises(OpError) as exc:
        apply_ops(
            scene,
            [
                {
                    "op": "add_atom_site",
                    "element": "H",
                    "site": {"type": "hollow", "anchors": ["aPd1", "aPd2"]},
                }
            ],
        )
    assert "hollow" in str(exc.value)
    assert "3" in str(exc.value)


def test_add_atom_site_duplicate_anchors_raises() -> None:
    from precis.structure import OpError

    scene = _pd_triangle()
    with pytest.raises(OpError):
        apply_ops(
            scene,
            [
                {
                    "op": "add_atom_site",
                    "element": "H",
                    "site": {"type": "bridge", "anchors": ["aPd1", "aPd1"]},
                }
            ],
        )


def test_add_atom_site_unknown_type_raises() -> None:
    from precis.structure import OpError

    scene = _pd_triangle()
    with pytest.raises(OpError):
        apply_ops(
            scene,
            [
                {
                    "op": "add_atom_site",
                    "element": "H",
                    "site": {"type": "octahedral", "anchors": ["aPd1"]},
                }
            ],
        )


def test_add_atom_site_is_deterministic() -> None:
    pytest.importorskip("ase")
    scene_a = _pd_triangle()
    scene_b = _pd_triangle()
    op = {
        "op": "add_atom_site",
        "element": "H",
        "site": {"type": "bridge", "anchors": ["aPd1", "aPd2"]},
    }
    apply_ops(scene_a, [dict(op)])
    apply_ops(scene_b, [dict(op)])
    labels_a = [la for la in scene_a.atoms if la not in ("aPd1", "aPd2", "aPd3")]
    labels_b = [la for la in scene_b.atoms if la not in ("aPd1", "aPd2", "aPd3")]
    assert np.allclose(scene_a.atoms[labels_a[0]].frac, scene_b.atoms[labels_b[0]].frac)


def test_add_atom_site_placed_atom_is_not_floating() -> None:
    """The whole point of the fix: the resolved atom passes the geometry
    lint that flags an untethered adsorbate (preflight's ``detached`` check)."""
    pytest.importorskip("ase")
    from precis.structure import preflight

    scene = _pd_triangle()
    apply_ops(
        scene,
        [
            {
                "op": "add_atom_site",
                "element": "H",
                "site": {
                    "type": "hollow",
                    "anchors": ["aPd1", "aPd2", "aPd3"],
                },
            }
        ],
    )
    verdict = preflight.preflight(scene)
    detached = [r for r in verdict.reasons if r.code == "detached"]
    assert detached == []


def test_unknown_op_and_bad_ref_raise() -> None:
    from precis.structure import OpError

    scene = Scene(cell=_cubic())
    with pytest.raises(OpError):
        apply_ops(scene, [{"op": "nope"}])
    with pytest.raises(OpError):
        apply_ops(scene, [{"op": "vacancy", "atom": "aXx9"}])


# -- probes ------------------------------------------------------------------


def test_neighbors_coordination_and_detect_bonds() -> None:
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},  # 2.6 Å
        ],
    )
    nbrs = probe.neighborhood(scene, "aPd1", radius=3.0)
    assert [n.label for n in nbrs] == ["aPd2"]
    assert nbrs[0].distance == pytest.approx(2.6)
    assert probe.coordination(scene, "aPd1") == 1  # within Pd-Pd cutoff 3.3 Å
    detected = probe.detect_bonds(scene)
    assert len(detected) == 1 and detected[0].provenance == "inferred"


def test_angle_is_mic_aware() -> None:
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "H", "frac": [0.6, 0.5, 0.5]},
            {"op": "add_atom", "element": "H", "frac": [0.5, 0.6, 0.5]},
        ],
    )
    assert probe.angle(scene, "aH1", "aO1", "aH2") == pytest.approx(90.0)
    assert probe.distance(scene, "aO1", "aH1") == pytest.approx(1.0)


def test_find_and_toc() -> None:
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0, 0, 0]},
            {"op": "add_atom", "element": "Cu", "frac": [0.5, 0.5, 0.5]},
        ],
    )
    assert probe.find(scene, element="Pd") == ["aPd1"]
    t = probe.toc(scene)
    assert t["natoms"] == 2
    assert t["formula"] == "Cu1Pd1"


# -- validator gate ----------------------------------------------------------


def test_validate_flags_overlap() -> None:
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "H", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "frac": [0.03, 0.0, 0.0]},  # 0.3 Å
        ],
    )
    findings = validate(scene)
    assert any(f.rule == "atom_overlap" for f in findings)


def test_validate_flags_over_valence() -> None:
    scene = Scene(cell=_cubic())
    ops: list[dict[str, object]] = [
        {"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]}
    ]
    # five H crowded around the carbon (each < C-H cutoff 1.28 Å)
    for dx, dy, dz in [
        (0.09, 0, 0),
        (-0.09, 0, 0),
        (0, 0.09, 0),
        (0, -0.09, 0),
        (0, 0, 0.09),
    ]:
        ops.append(
            {"op": "add_atom", "element": "H", "frac": [0.5 + dx, 0.5 + dy, 0.5 + dz]}
        )
    apply_ops(scene, ops)
    findings = validate(scene)
    over = [f for f in findings if f.rule == "over_valence"]
    assert over and over[0].atoms == ["aC1"] and over[0].measured == 5


def test_validate_over_valence_ignores_metal_surface_coordination() -> None:
    # H adsorbate sitting in a Pd(111)-style hollow site — within the H-Pd
    # bond cutoff (2.04 Å at tolerance 1.2) of three slab Pd atoms. Metal
    # neighbours aren't covalent bonds, so this must NOT trip over_valence —
    # hollow/bridge adsorption is the chemically preferred geometry.
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "H", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "Pd", "frac": [0.68, 0.5, 0.5]},  # 1.8 Å
            {"op": "add_atom", "element": "Pd", "frac": [0.41, 0.6559, 0.5]},  # 1.8 Å
            {"op": "add_atom", "element": "Pd", "frac": [0.41, 0.3441, 0.5]},  # 1.8 Å
        ],
    )
    assert probe.coordination(scene, "aH1") == 3  # raw count still sees the metals
    assert probe.covalent_coordination(scene, "aH1") == 0
    assert validate(scene) == []


def test_validate_over_valence_still_fires_between_covalent_elements() -> None:
    # Same shape of crowding, but with covalent (non-metal) neighbours — the
    # rule must still catch it.
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "H", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.62, 0.5, 0.5]},  # 1.2 Å
            {"op": "add_atom", "element": "C", "frac": [0.38, 0.5, 0.5]},  # 1.2 Å
        ],
    )
    over = [f for f in validate(scene) if f.rule == "over_valence"]
    assert over and over[0].atoms == ["aH1"] and over[0].measured == 2


def test_validate_bond_too_long_boundary() -> None:
    # O-H covalent sum 0.97 Å, ceiling = 1.3× = 1.261 Å.
    just_inside = Scene(cell=_cubic())
    apply_ops(
        just_inside,
        [
            {"op": "add_atom", "element": "O", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "frac": [0.125, 0.0, 0.0]},  # 1.25 Å
            {"op": "add_bond", "i": "aO1", "j": "aH1", "order": 1},
        ],
    )
    assert not any(f.rule == "bond_too_long" for f in validate(just_inside))

    just_outside = Scene(cell=_cubic())
    apply_ops(
        just_outside,
        [
            {"op": "add_atom", "element": "O", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "frac": [0.127, 0.0, 0.0]},  # 1.27 Å
            {"op": "add_bond", "i": "aO1", "j": "aH1", "order": 1},
        ],
    )
    findings = [f for f in validate(just_outside) if f.rule == "bond_too_long"]
    assert findings and findings[0].atoms == ["aO1", "aH1"]
    assert "aO1" in findings[0].suggested_fix and "aH1" in findings[0].suggested_fix


def test_validate_ignores_inferred_bond_length() -> None:
    # An auto-detected bond can never actually reach the reject ceiling (its
    # own detection cutoff is tighter), but pin the provenance filter
    # directly so a future change to the detection cutoff can't silently
    # start flagging inferred bonds too.
    from precis.structure.scene import Bond

    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "O", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "frac": [0.3, 0.0, 0.0]},  # 3 Å
        ],
    )
    scene.bonds.append(Bond(i="aO1", j="aH1", provenance="inferred"))
    assert not any(f.rule == "bond_too_long" for f in validate(scene))


def test_validate_flags_bond_order_exceeding_valence() -> None:
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "O", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "C", "frac": [0.1, 0.0, 0.0]},  # 1.0 Å
            # a triple bond on O — max valence 2, impossible.
            {"op": "add_bond", "i": "aO1", "j": "aC1", "order": 3},
        ],
    )
    findings = [f for f in validate(scene) if f.rule == "bond_order_exceeds_valence"]
    assert findings and set(findings[0].atoms) == {"aO1", "aC1"}


def test_validate_bond_order_finding_deduped_when_both_endpoints_exceed() -> None:
    # O (max valence 2) - F (max valence 1): an order-3 bond exceeds BOTH
    # endpoints' valence, but must still report exactly one finding.
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "O", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "F", "frac": [0.1, 0.0, 0.0]},  # 1.0 Å
            {"op": "add_bond", "i": "aO1", "j": "aF1", "order": 3},
        ],
    )
    findings = [f for f in validate(scene) if f.rule == "bond_order_exceeds_valence"]
    assert len(findings) == 1
    assert set(findings[0].atoms) == {"aO1", "aF1"}


def test_validate_flags_valence_budget_exceeded() -> None:
    # Three double bonds on one carbon: no single order exceeds C's max
    # valence 4, but the sum (6) does — the budget rule's whole point.
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "O", "frac": [0.62, 0.5, 0.5]},
            {"op": "add_atom", "element": "O", "frac": [0.38, 0.5, 0.5]},
            {"op": "add_atom", "element": "O", "frac": [0.5, 0.62, 0.5]},
            {"op": "add_bond", "i": "aC1", "j": "aO1", "order": 2},
            {"op": "add_bond", "i": "aC1", "j": "aO2", "order": 2},
            {"op": "add_bond", "i": "aC1", "j": "aO3", "order": 2},
        ],
    )
    findings = [f for f in validate(scene) if f.rule == "valence_budget_exceeded"]
    assert len(findings) == 1
    assert findings[0].atoms == ["aC1"]
    assert findings[0].measured == 6
    assert findings[0].expected == 4
    # the O endpoints are each within budget (2 <= 2) — no finding on them.


def test_validate_valence_budget_allows_aromatic_fractional_sum() -> None:
    # Benzene-carbon budget: 1.5 + 1.5 + 1 = 4 = C's max valence — legal.
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.64, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.36, 0.5, 0.5]},
            {"op": "add_atom", "element": "H", "frac": [0.5, 0.61, 0.5]},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 1.5},
            {"op": "add_bond", "i": "aC1", "j": "aC3", "order": 1.5},
            {"op": "add_bond", "i": "aC1", "j": "aH1", "order": 1},
        ],
    )
    assert not any(f.rule == "valence_budget_exceeded" for f in validate(scene))


def test_validate_valence_budget_tolerates_float32_pauling_roundtrip() -> None:
    # gripe 279306: a Pauling order 4/3 round-tripped through the ``real``
    # (float32) ``struct_bonds.bond_order`` storage column comes back as
    # float32's nearest representable value, not float64's exact
    # 1.3333333333333333 — simulate that round-trip directly rather than
    # going through the store, so this test still fails loud if the
    # epsilon regresses without needing a live DB.
    order = float(np.float32(4.0 / 3.0))
    assert order > 4.0 / 3.0  # sanity: the round-trip really did drift up
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.64, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.36, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.5, 0.61, 0.5]},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": order},
            {"op": "add_bond", "i": "aC1", "j": "aC3", "order": order},
            {"op": "add_bond", "i": "aC1", "j": "aC4", "order": order},
        ],
    )
    assert not any(f.rule == "valence_budget_exceeded" for f in validate(scene))


def test_validate_valence_budget_still_flags_three_aromatic_1_5_bonds() -> None:
    # The epsilon must not swallow a genuine over-valence: 3 x 1.5 = 4.5 is
    # well past both C's max valence 4 and the float32-roundtrip epsilon.
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.64, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.36, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.5, 0.61, 0.5]},
            {"op": "add_bond", "i": "aC1", "j": "aC2", "order": 1.5},
            {"op": "add_bond", "i": "aC1", "j": "aC3", "order": 1.5},
            {"op": "add_bond", "i": "aC1", "j": "aC4", "order": 1.5},
        ],
    )
    findings = [f for f in validate(scene) if f.rule == "valence_budget_exceeded"]
    assert len(findings) == 1
    assert findings[0].atoms == ["aC1"]
    assert findings[0].measured == 4.5


def test_validate_valence_budget_defers_to_single_bond_finding() -> None:
    # A lone order-3 bond on O already fires bond_order_exceeds_valence;
    # the budget rule must not double-report the same root cause.
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "O", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "C", "frac": [0.1, 0.0, 0.0]},
            {"op": "add_bond", "i": "aO1", "j": "aC1", "order": 3},
        ],
    )
    findings = validate(scene)
    assert any(f.rule == "bond_order_exceeds_valence" for f in findings)
    assert not any(f.rule == "valence_budget_exceeded" for f in findings)


def test_validate_valence_budget_ignores_inferred_bonds() -> None:
    # Inferred bonds carry a guessed nominal order — only declared bonds
    # count toward the budget.
    from precis.structure.scene import Bond

    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "O", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "frac": [0.1, 0.0, 0.0]},
            {"op": "add_atom", "element": "H", "frac": [0.9, 0.0, 0.0]},
            {"op": "add_bond", "i": "aO1", "j": "aH1", "order": 1},
        ],
    )
    scene.bonds.append(Bond(i="aO1", j="aH2", order=2.0, provenance="inferred"))
    assert not any(f.rule == "valence_budget_exceeded" for f in validate(scene))


# -- export (pure formats) ---------------------------------------------------


def test_validate_charged_quaternary_ammonium_passes_via_declared_charge() -> None:
    # gr285775: a quaternary N+ (4 single bonds) must pass because it
    # DECLARES charge=+1, not because neutral N's table entry happens to be
    # 4 too. CBPQT(4+)'s N+ centres are the motivating case.
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {
                "op": "add_atom",
                "element": "N",
                "frac": [0.5, 0.5, 0.5],
                "charge": 1,
            },
            {"op": "add_atom", "element": "C", "frac": [0.62, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.38, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.5, 0.62, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.5, 0.38, 0.5]},
            {"op": "add_bond", "i": "aN1", "j": "aC1", "order": 1},
            {"op": "add_bond", "i": "aN1", "j": "aC2", "order": 1},
            {"op": "add_bond", "i": "aN1", "j": "aC3", "order": 1},
            {"op": "add_bond", "i": "aN1", "j": "aC4", "order": 1},
        ],
    )
    assert scene.atoms["aN1"].charge == 1
    findings = validate(scene)
    assert not any(f.atoms == ["aN1"] for f in findings)


def test_validate_charged_n_plus_still_flags_a_fifth_bond() -> None:
    # Proves the explicit charged-valence table is used, not naive
    # `max_valence('N') + charge` arithmetic (4 + 1 = 5 would wrongly permit
    # a 5th bond) — N+'s real effective budget is 4, so a 5th single bond
    # must still trip the budget rule.
    scene = Scene(cell=_cubic())
    ops: list[dict[str, object]] = [
        {"op": "add_atom", "element": "N", "frac": [0.5, 0.5, 0.5], "charge": 1}
    ]
    for i, (dx, dy, dz) in enumerate(
        [(0.12, 0, 0), (-0.12, 0, 0), (0, 0.12, 0), (0, -0.12, 0), (0, 0, 0.12)]
    ):
        ops.append(
            {"op": "add_atom", "element": "C", "frac": [0.5 + dx, 0.5 + dy, 0.5 + dz]}
        )
        ops.append({"op": "add_bond", "i": "aN1", "j": f"aC{i + 1}", "order": 1})
    apply_ops(scene, ops)
    findings = [f for f in validate(scene) if f.atoms == ["aN1"]]
    assert findings and findings[0].rule in (
        "over_valence",
        "valence_budget_exceeded",
    )
    assert findings[0].expected == 4


def test_validate_carboxylate_o_minus_single_bond_is_clean() -> None:
    # gr285775: an O- (charge=-1) with exactly one declared bond — a
    # carboxylate/alkoxide oxygen — must validate clean; nothing in the
    # error-tier gate polices under-valence at all, so this is really a
    # regression guard against ever adding one without charge-awareness.
    scene = Scene(cell=_cubic())
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5], "charge": -1},
            {"op": "add_atom", "element": "C", "frac": [0.6, 0.5, 0.5]},
            {"op": "add_bond", "i": "aO1", "j": "aC1", "order": 1},
        ],
    )
    findings = validate(scene)
    assert not any(f.atoms == ["aO1"] for f in findings)


def test_validate_o_plus_three_bonds_only_passes_when_charged() -> None:
    # Demonstrates the charge-aware budget actually changes the outcome
    # (unlike the N+ case, where neutral N's table entry already matches):
    # neutral O's max valence is 2, so 3 single bonds fails; declaring O+
    # (effective budget 3) makes the identical bond graph pass.
    def _three_bonded_oxygen(charge: int) -> Scene:
        scene = Scene(cell=_cubic())
        ops: list[dict[str, object]] = [
            {
                "op": "add_atom",
                "element": "O",
                "frac": [0.5, 0.5, 0.5],
                "charge": charge,
            }
        ]
        for i, (dx, dy, dz) in enumerate([(0.12, 0, 0), (-0.12, 0, 0), (0, 0.12, 0)]):
            ops.append(
                {
                    "op": "add_atom",
                    "element": "C",
                    "frac": [0.5 + dx, 0.5 + dy, 0.5 + dz],
                }
            )
            ops.append({"op": "add_bond", "i": "aO1", "j": f"aC{i + 1}", "order": 1})
        apply_ops(scene, ops)
        return scene

    neutral_findings = [
        f for f in validate(_three_bonded_oxygen(0)) if f.atoms == ["aO1"]
    ]
    assert neutral_findings  # 3 > neutral O's max valence 2

    charged_findings = [
        f for f in validate(_three_bonded_oxygen(1)) if f.atoms == ["aO1"]
    ]
    assert not charged_findings  # O+'s effective budget is 3


def test_poscar_export_groups_and_selective_dynamics() -> None:
    scene = Scene(cell=_cubic(3.0))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "H", "frac": [0.25, 0.25, 0.25]},
            {"op": "constrain", "atoms": ["aH1"], "kind": "fixed-all"},
        ],
    )
    lines = export.to_poscar(scene).splitlines()
    assert lines[0] == "Pd2H1"
    assert lines[5].split() == ["Pd", "H"]
    assert lines[6].split() == ["2", "1"]
    assert "Selective dynamics" in lines
    assert any(line.endswith("F F F") for line in lines)  # the fixed H
    assert any(line.endswith("T T T") for line in lines)  # the free Pd


def test_extxyz_export_is_cartesian_with_labels() -> None:
    scene = Scene(cell=_cubic(10.0))
    apply_ops(scene, [{"op": "add_atom", "element": "O", "frac": [0.1, 0.2, 0.3]}])
    lines = export.to_extxyz(scene).splitlines()
    assert lines[0] == "1"
    assert "Lattice=" in lines[1] and 'pbc="T T T"' in lines[1]
    parts = lines[2].split()
    assert parts[0] == "O" and parts[4] == "aO1"
    assert float(parts[1]) == pytest.approx(1.0)  # 0.1 frac × 10 Å


# -- relax (rung 0, pure) ----------------------------------------------------


def test_relax_clean_separates_overlap() -> None:
    scene = Scene(cell=_cubic(20.0))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.05, 0.0, 0.0]},  # 1.0 Å
        ],
    )
    assert probe.distance(scene, "aPd1", "aPd2") == pytest.approx(1.0)
    res = relax(scene, fidelity="clean")
    assert res.converged and res.rung == "clean"
    # pushed apart toward the Pd-Pd covalent length (~2.78 Å)
    assert probe.distance(scene, "aPd1", "aPd2") >= 2.7


def test_relax_clean_respects_fixed() -> None:
    scene = Scene(cell=_cubic(20.0))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.05, 0.0, 0.0]},
            {"op": "constrain", "atoms": ["aPd1"], "kind": "fixed-all"},
        ],
    )
    relax(scene, fidelity="clean")
    assert np.allclose(scene.atoms["aPd1"].frac, [0.0, 0.0, 0.0])  # never moved


def test_relax_rented_rungs_are_gated(monkeypatch) -> None:
    # The gate container installs the [dft-ml] extra (Dockerfile `uv sync
    # --all-extras`), so the 'ml' rung has a real MACE backend and would relax
    # inline. Force the MLIP absent so 'ml' is gated like the other rented
    # rungs — the data-host condition these rungs are designed around.
    import importlib

    # NB: the ``precis.structure`` package re-exports the ``relax`` *function*,
    # shadowing the submodule name — reach the module via importlib.
    relax_mod = importlib.import_module("precis.structure.relax")

    def _no_mlip(model, **_kw):
        raise RelaxUnsupported("no MLIP backend (test)")

    monkeypatch.setattr(relax_mod, "_ml_calculator", _no_mlip)
    scene = Scene(cell=_cubic(20.0))
    apply_ops(scene, [{"op": "add_atom", "element": "Pd", "frac": [0, 0, 0]}])
    for rung in ("ml", "dft-fast", "xtb"):
        with pytest.raises(RelaxUnsupported):
            relax(scene, fidelity=rung)


def test_cell_masks_pin_the_c_axis_inplane() -> None:
    # In-plane frees a/b + the γ shear (Voigt xx, yy, xy) and pins zz/yz/xz so a
    # slab's vacuum can't collapse; full frees all six strain components.
    from precis.structure.relax import _CELL_MASKS

    assert _CELL_MASKS["inplane"] == [True, True, False, False, False, True]
    assert all(_CELL_MASKS["full"])


def test_relax_cell_mode_needs_an_energy_rung() -> None:
    scene = Scene(cell=_cubic(10.0))
    apply_ops(scene, [{"op": "add_atom", "element": "Pd", "frac": [0, 0, 0]}])
    with pytest.raises(RelaxUnsupported):  # 'clean' has no stress
        relax(scene, fidelity="clean", cell="inplane")


# -- relax (rung 1, ASE-EMT) -------------------------------------


def _pd_slab() -> Scene:
    """A small fcc111 2x2x3 Pd slab (ASE-built), imported into a Scene."""
    pytest.importorskip("ase")
    from ase.build import fcc111

    atoms = fcc111("Pd", size=(2, 2, 3), vacuum=10.0)
    cell = Cell(np.asarray(atoms.get_cell()), pbc=tuple(atoms.pbc))
    scene = Scene(cell=cell)
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": sym, "cart": list(map(float, pos))}
            for sym, pos in zip(atoms.get_chemical_symbols(), atoms.get_positions())
        ],
    )
    return scene


def test_relax_emt_converges_and_moves_a_pd_slab() -> None:
    pytest.importorskip("ase")
    scene = _pd_slab()
    before = {la: a.frac.copy() for la, a in scene.atoms.items()}
    res = relax(scene, fidelity="emt", steps=200, tol=0.05)
    assert res.rung == "emt"
    assert res.converged
    assert res.energy is not None and res.max_force is not None
    moved = any(
        not np.allclose(before[la], scene.atoms[la].frac, atol=1e-6)
        for la in scene.atoms
    )
    assert moved


def test_relax_emt_respects_fixed() -> None:
    pytest.importorskip("ase")
    from precis.structure.scene import FIX_ALL

    scene = _pd_slab()
    fixed_label = next(iter(scene.atoms))
    scene.atoms[fixed_label].fixed = FIX_ALL
    before = scene.atoms[fixed_label].frac.copy()
    relax(scene, fidelity="emt", steps=50, tol=0.05)
    assert np.allclose(scene.atoms[fixed_label].frac, before, atol=1e-9)


def test_relax_emt_rejects_out_of_set_element() -> None:
    pytest.importorskip("ase")
    scene = Scene(cell=_cubic(20.0))
    apply_ops(scene, [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.0]}])
    with pytest.raises(RelaxUnsupported) as exc:
        relax(scene, fidelity="emt")
    msg = str(exc.value)
    assert "EMT covers" in msg
    assert "fidelity='ml'" in msg


# -- per-atom forces (gripe 161576) ------------------------------------------


def test_relax_emt_records_real_per_atom_forces() -> None:
    """A real emt relax carries a per-atom force dict, not the cheap estimate."""
    pytest.importorskip("ase")
    scene = _pd_slab()
    res = relax(scene, fidelity="emt", steps=200, tol=0.05)
    assert res.forces is not None
    assert set(res.forces) == set(scene.atoms)
    assert all(len(v) == 3 for v in res.forces.values())
    assert res.forces_approx is False
    assert res.forces_source == "emt"


def test_relax_ml_records_real_per_atom_forces(monkeypatch) -> None:
    """Rung 'ml' also carries real per-atom forces, labeled by its own model."""
    pytest.importorskip("ase")
    import importlib

    from ase.calculators.emt import EMT

    relax_mod = importlib.import_module("precis.structure.relax")
    monkeypatch.setattr(relax_mod, "_ml_calculator", lambda model, **_kw: EMT())

    scene = Scene(cell=_cubic(10.0))
    apply_ops(scene, [{"op": "add_atom", "element": "Cu", "frac": [0.0, 0.0, 0.0]}])
    res = relax(scene, fidelity="ml", steps=20, model="mace_mp")
    assert res.forces is not None
    assert res.forces_approx is False
    assert res.forces_source == "mace_mp"


def test_estimate_forces_emt_supported_elements() -> None:
    """The cheap always-available estimate (rung 0 has no calculator) — real
    numbers for an EMT-covered element set."""
    pytest.importorskip("ase")
    from precis.structure.relax import estimate_forces_emt

    scene = Scene(cell=_cubic(10.0))
    apply_ops(scene, [{"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]}])
    est = estimate_forces_emt(scene)
    assert est is not None
    assert set(est) == {"aPd1"}
    assert len(est["aPd1"]) == 3


def test_estimate_forces_emt_unsupported_elements_is_none() -> None:
    """Never fabricate: an element outside EMT's closed coverage ⇒ None."""
    pytest.importorskip("ase")
    from precis.structure.relax import estimate_forces_emt

    scene = Scene(cell=_cubic(10.0))
    apply_ops(scene, [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.0]}])
    assert estimate_forces_emt(scene) is None


def test_relax_clean_surfaces_approx_forces_on_supported_elements() -> None:
    """Rung 'clean' has no calculator of its own, but on an EMT-supported
    element set it still surfaces a labeled-approximate force estimate."""
    pytest.importorskip("ase")
    scene = Scene(cell=_cubic(10.0))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.2, 0.0, 0.0]},
        ],
    )
    res = relax(scene, fidelity="clean", steps=50)
    assert res.forces is not None
    assert res.forces_approx is True
    assert res.forces_source == "emt"


def test_relax_clean_no_forces_outside_emt_coverage() -> None:
    """Rung 'clean' on an unsupported element set surfaces no forces at all
    — never a fabricated number."""
    scene = Scene(cell=_cubic(10.0))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Fe", "frac": [0.2, 0.0, 0.0]},
        ],
    )
    res = relax(scene, fidelity="clean", steps=50)
    assert res.forces is None
    assert res.forces_approx is False


def test_relax_unknown_cell_mode_raises() -> None:
    scene = Scene(cell=_cubic(10.0))
    apply_ops(scene, [{"op": "add_atom", "element": "Pd", "frac": [0, 0, 0]}])
    with pytest.raises(RelaxUnsupported):
        relax(scene, fidelity="ml", cell="sideways")


def test_relax_ml_inplane_relaxes_the_box_but_pins_the_vacuum(monkeypatch) -> None:
    # A genuine variable-cell relax via the fast EMT calculator (no MACE needed):
    # the in-plane lattice is free to move off a deliberately-strained value while
    # the c-axis (the vacuum gap) stays pinned by the mask.
    pytest.importorskip("ase")
    import importlib

    from ase.calculators.emt import EMT

    relax_mod = importlib.import_module("precis.structure.relax")
    monkeypatch.setattr(relax_mod, "_ml_calculator", lambda model, **_kw: EMT())

    scene = Scene(cell=Cell(np.diag([5.0, 5.0, 12.0]), pbc=(True, True, False)))
    apply_ops(scene, [{"op": "add_atom", "element": "Cu", "frac": [0.0, 0.0, 0.5]}])
    c_before = float(scene.cell.lattice[2][2])
    res = relax(scene, fidelity="ml", cell="inplane", steps=40)

    assert res.rung == "ml" and res.energy is not None
    assert float(scene.cell.lattice[2][2]) == pytest.approx(c_before)  # vacuum pinned
    # the in-plane vectors moved off the strained 5.0 Å (the box relaxed)
    assert float(scene.cell.lattice[0][0]) != pytest.approx(5.0)


def test_relax_dispersion_needs_the_ml_rung() -> None:
    """D3 corrects a *potential*; the geometry-repair and EMT rungs have none,
    so asking there raises rather than being silently dropped."""
    scene = Scene(cell=_cubic(10.0))
    apply_ops(scene, [{"op": "add_atom", "element": "Pd", "frac": [0, 0, 0]}])
    for rung in ("clean", "emt"):
        with pytest.raises(RelaxUnsupported):
            relax(scene, fidelity=rung, dispersion=True)


def test_relax_dispersion_reaches_the_calculator_and_is_recorded(monkeypatch) -> None:
    """``dispersion`` is threaded to ``_ml_calculator`` (not dropped between
    the public signature and the backend) and stamped on the result, which is
    what the handler keys the run-cube on."""
    pytest.importorskip("ase")
    import importlib

    from ase.calculators.emt import EMT

    relax_mod = importlib.import_module("precis.structure.relax")
    seen: list[bool] = []

    def _spy(model, *, dispersion=False):
        seen.append(dispersion)
        return EMT()

    monkeypatch.setattr(relax_mod, "_ml_calculator", _spy)

    scene = Scene(cell=_cubic(10.0))
    apply_ops(scene, [{"op": "add_atom", "element": "Cu", "frac": [0, 0, 0]}])
    res = relax(scene, fidelity="ml", dispersion=True, steps=5)
    assert seen == [True]
    assert res.dispersion is True

    res_off = relax(scene, fidelity="ml", steps=5)
    assert seen == [True, False]  # default stays off
    assert res_off.dispersion is False


def test_ml_calculator_dispersion_needs_torch_dftd(monkeypatch) -> None:
    """Without ``torch-dftd`` a dispersion request is one clean
    RelaxUnsupported — never a relax that quietly ran uncorrected."""
    import builtins
    import importlib

    pytest.importorskip("mace")
    relax_mod = importlib.import_module("precis.structure.relax")
    real_import = builtins.__import__

    def _no_dftd(name, *args, **kwargs):
        if name == "torch_dftd":
            raise ImportError("no torch_dftd (test)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_dftd)
    with pytest.raises(RelaxUnsupported, match="torch-dftd"):
        relax_mod._ml_calculator("mace_mp", dispersion=True)


def test_ml_calculator_chgnet_rejects_dispersion() -> None:
    """CHGNet has no D3 to add — say so instead of ignoring the flag."""
    import importlib

    relax_mod = importlib.import_module("precis.structure.relax")
    with pytest.raises(RelaxUnsupported, match="no dispersion"):
        relax_mod._ml_calculator("chgnet", dispersion=True)


# -- gripe 285774: organic-domain model + composition routing ---------------


def test_ml_calculator_mace_off_needs_the_dft_ml_extra(monkeypatch) -> None:
    """Same clean-``RelaxUnsupported`` contract as mace_mp/chgnet when the
    backend package isn't installed — never a stray ImportError."""
    import builtins
    import importlib

    relax_mod = importlib.import_module("precis.structure.relax")
    real_import = builtins.__import__

    def _no_mace(name, *args, **kwargs):
        if name == "mace" or name.startswith("mace."):
            raise ImportError("no mace (test)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_mace)
    with pytest.raises(RelaxUnsupported, match=r"\[dft-ml\]"):
        relax_mod._ml_calculator("mace_off", dispersion=False)


def test_ml_calculator_mace_off_dispersion_needs_torch_dftd(monkeypatch) -> None:
    """mace_off has no built-in ``dispersion`` kwarg (unlike mace_mp) — the
    D3 backend is still probed up front, same clean-failure contract."""
    import builtins
    import importlib

    pytest.importorskip("mace")
    relax_mod = importlib.import_module("precis.structure.relax")
    real_import = builtins.__import__

    def _no_dftd(name, *args, **kwargs):
        if name == "torch_dftd":
            raise ImportError("no torch_dftd (test)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_dftd)
    with pytest.raises(RelaxUnsupported, match="torch-dftd"):
        relax_mod._ml_calculator("mace_off", dispersion=True)


def test_route_ml_model_organic_neutral_nonperiodic_picks_mace_off() -> None:
    scene = Scene(cell=_cubic(20.0, pbc=(False, False, False)))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "H", "frac": [0.52, 0.5, 0.5]},
            {"op": "add_atom", "element": "O", "frac": [0.48, 0.5, 0.5]},
        ],
    )
    assert route_ml_model(scene) == "mace_off"


def test_route_ml_model_periodic_metal_keeps_mace_mp() -> None:
    """A periodic structure keeps the materials default even when every
    element is technically organic-coverage (e.g. a lone carbon lattice)."""
    scene = Scene(cell=_cubic(10.0, pbc=(True, True, True)))
    apply_ops(scene, [{"op": "add_atom", "element": "Pd", "frac": [0, 0, 0]}])
    assert route_ml_model(scene) == "mace_mp"

    periodic_organic = Scene(cell=_cubic(10.0, pbc=(True, True, False)))
    apply_ops(periodic_organic, [{"op": "add_atom", "element": "C", "frac": [0, 0, 0]}])
    assert route_ml_model(periodic_organic) == "mace_mp"


def test_route_ml_model_charged_species_keeps_mace_mp() -> None:
    """A declared net charge falls back to the materials default — neither
    registered model was trained with charge states in mind, and 'mace_mp'
    is the conservative, historical choice."""
    scene = Scene(cell=_cubic(20.0, pbc=(False, False, False)))
    apply_ops(scene, [{"op": "add_atom", "element": "N", "frac": [0.5, 0.5, 0.5]}])
    scene.atoms["aN1"].oxidation = 1
    assert route_ml_model(scene) == "mace_mp"


def test_route_ml_model_mixed_elements_keeps_mace_mp() -> None:
    """A metal-organic straddle (element set outside :data:`ORGANIC_ELEMENTS`)
    keeps the materials default — never a silent guess toward the organic
    model for chemistry it wasn't trained on."""
    scene = Scene(cell=_cubic(20.0, pbc=(False, False, False)))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.52, 0.5, 0.5]},
        ],
    )
    assert route_ml_model(scene) == "mace_mp"


def test_route_ml_model_empty_scene_keeps_mace_mp() -> None:
    assert route_ml_model(Scene(cell=_cubic(10.0))) == "mace_mp"


# -- nav probes: spatial (line / plane / sphere, §6.2) -----------------------


def _chain(cell: Cell, els_fracs: list[tuple[str, list[float]]]) -> Scene:
    scene = Scene(cell=cell)
    apply_ops(
        scene,
        [{"op": "add_atom", "element": e, "frac": f} for e, f in els_fracs],
    )
    return scene


def test_line_probe_orders_atoms_along_ray() -> None:
    scene = _chain(
        _cubic(10.0),
        [("C", [0.1, 0.5, 0.5]), ("C", [0.5, 0.5, 0.5]), ("C", [0.9, 0.51, 0.5])],
    )
    hits = probe.line(
        scene, np.array([0.0, 5.0, 5.0]), np.array([1.0, 0.0, 0.0]), radius=0.5
    )
    assert [h.label for h in hits] == ["aC1", "aC2", "aC3"]
    assert hits[0].along == pytest.approx(1.0)
    assert hits[2].offset == pytest.approx(0.1, abs=1e-6)  # 0.01 frac × 10 Å


def test_plane_probe_returns_layer_slice() -> None:
    scene = _chain(
        _cubic(10.0),
        [("Pd", [0.2, 0.2, 0.5]), ("Pd", [0.8, 0.3, 0.5]), ("Pd", [0.5, 0.5, 0.9])],
    )
    hits = probe.plane(
        scene, np.array([0.0, 0.0, 5.0]), np.array([0.0, 0.0, 1.0]), thickness=0.5
    )
    assert {h.label for h in hits} == {"aPd1", "aPd2"}  # the z=0.5 layer, not z=0.9


def test_bonds_through_plane_finds_interlayer_bond() -> None:
    scene = Scene(cell=_cubic(10.0))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0.5, 0.5, 0.4]},
            {"op": "add_atom", "element": "Pd", "frac": [0.5, 0.5, 0.6]},
            {"op": "add_bond", "i": "aPd1", "j": "aPd2", "order": 1},
        ],
    )
    crossing = probe.bonds_through_plane(
        scene, np.array([0, 0, 5.0]), np.array([0, 0, 1.0])
    )
    assert len(crossing) == 1
    assert crossing[0].angle_to_normal == pytest.approx(0.0, abs=1e-6)  # straight up


def test_bonds_in_sphere_captures_local_bonds() -> None:
    scene = Scene(cell=_cubic(10.0))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "O", "frac": [0.6, 0.5, 0.5]},
            {"op": "add_bond", "i": "aC1", "j": "aO1", "order": 2},
        ],
    )
    inside = probe.bonds_in_sphere(scene, np.array([5.5, 5.0, 5.0]), radius=1.0)
    assert len(inside) == 1 and inside[0].order == 2.0


# -- nav probes: graph topology (path / rings / fragments, §6.1/§6.5) --------


def test_path_and_fragments() -> None:
    scene = Scene(cell=_cubic(10.0))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "frac": [0.1, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.2, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.3, 0.5, 0.5]},
            {"op": "add_atom", "element": "O", "frac": [0.8, 0.5, 0.5]},  # island
            {"op": "add_bond", "i": "aC1", "j": "aC2"},
            {"op": "add_bond", "i": "aC2", "j": "aC3"},
        ],
    )
    assert probe.path(scene, "aC1", "aC3") == ["aC1", "aC2", "aC3"]
    assert probe.path(scene, "aC1", "aO1") is None
    frags = probe.fragments(scene)
    assert [len(f) for f in frags] == [3, 1]


def test_rings_finds_benzene_hexagon() -> None:
    scene = Scene(cell=_cubic(10.0))
    # six carbons in a ring (positions need not be ideal; the graph is the ring)
    ops: list[dict[str, object]] = [
        {"op": "add_atom", "element": "C", "frac": [0.5 + 0.05 * x, 0.5, 0.5]}
        for x in range(6)
    ]
    apply_ops(scene, ops)
    bonds = [
        {"op": "add_bond", "i": f"aC{i + 1}", "j": f"aC{(i + 1) % 6 + 1}"}
        for i in range(6)
    ]
    apply_ops(scene, bonds)
    found = probe.rings(scene, max_size=8)
    assert any(len(r) == 6 for r in found)


# -- diff + dihedral + pov ---------------------------------------------------


def test_diff_reports_displacement_and_graph_delta() -> None:
    before = _chain(_cubic(10.0), [("Pd", [0.5, 0.5, 0.5]), ("Pd", [0.6, 0.5, 0.5])])
    after = _chain(_cubic(10.0), [("Pd", [0.5, 0.5, 0.5]), ("Pd", [0.7, 0.5, 0.5])])
    apply_ops(after, [{"op": "add_atom", "element": "H", "frac": [0.5, 0.5, 0.6]}])
    apply_ops(after, [{"op": "add_bond", "i": "aPd1", "j": "aPd2"}])
    d = probe.diff(before, after)
    assert d.atoms_added == ["aH1"]
    assert d.bonds_formed == [("aPd1", "aPd2")]
    assert d.max_disp == pytest.approx(1.0)  # aPd2 moved 0.1 frac × 10 Å


def test_dihedral_is_ninety_for_perpendicular() -> None:
    scene = _chain(
        _cubic(10.0),
        [
            ("C", [0.4, 0.6, 0.5]),  # off-axis so A–B–C is not collinear
            ("C", [0.5, 0.5, 0.5]),
            ("C", [0.6, 0.5, 0.5]),
            ("O", [0.6, 0.5, 0.6]),
        ],
    )
    assert abs(probe.dihedral(scene, "aC1", "aC2", "aC3", "aO1")) == pytest.approx(90.0)


def test_pov_uniform_readout() -> None:
    scene = Scene(cell=_cubic(10.0))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "O", "frac": [0.6, 0.5, 0.5]},  # 1.0 Å
            {"op": "add_atom", "element": "H", "frac": [0.9, 0.5, 0.5]},  # far
        ],
    )
    p = probe.pov(scene, ["aPd1"], reach=2.0)
    assert p.i_am == "atom" and p.i_include == ["aPd1"]
    assert [t[0] for t in p.i_touch] == ["aO1"]  # H is out of reach
    pf = probe.pov(scene, ["aPd1", "aO1"], reach=2.0)
    assert pf.i_am == "fragment" and "aPd1" not in dict(pf.i_touch)


# -- slab bulk template (§5b) ------------------------------------------------


def test_op_slab_builds_fcc111_with_frozen_bottom_layers() -> None:
    """The `slab` op re-seeds the scene with an fcc(111) surface and freezes the
    bottom `fix_layers` layers (mirrors autocatpath's build_slab so the geometry can
    be injected into a barrier run)."""
    pytest.importorskip("ase.build")
    from precis.structure.scene import FIX_ALL

    scene = Scene(cell=_cubic(1.0))  # placeholder cell; slab overwrites it
    apply_ops(
        scene,
        [
            {
                "op": "slab",
                "element": "Pd",
                "size": [2, 2, 3],
                "vacuum": 8.0,
                "fix_layers": 1,
            }
        ],
    )
    assert len(scene.atoms) == 12  # nx*ny*nz = 2*2*3
    assert {a.element for a in scene.atoms.values()} == {"Pd"}
    assert scene.cell.pbc == (True, True, True)
    # exactly one layer (nx*ny = 4 atoms) frozen, and it's the bottom (min z)
    frozen = [a for a in scene.atoms.values() if a.fixed == FIX_ALL]
    assert len(frozen) == 4
    zs = sorted(a.frac[2] for a in scene.atoms.values())
    frozen_zs = sorted(a.frac[2] for a in frozen)
    assert frozen_zs == zs[:4]  # the four lowest


def test_op_slab_needs_element_and_size() -> None:
    pytest.importorskip("ase.build")
    from precis.structure import OpError

    scene = Scene(cell=_cubic(1.0))
    with pytest.raises(OpError):
        apply_ops(scene, [{"op": "slab", "element": "Pd"}])  # no size
    with pytest.raises(OpError):
        apply_ops(scene, [{"op": "slab", "size": [2, 2, 3]}])  # no element


def test_op_slab_tolerates_null_optional_params() -> None:
    # An LLM often emits an explicit null for an optional param instead of
    # omitting the key; null must mean "default", not a raw TypeError crash.
    pytest.importorskip("ase.build")
    scene = Scene(cell=_cubic(1.0))
    apply_ops(
        scene,
        [
            {
                "op": "slab",
                "element": "Pd",
                "size": [2, 2, 3],
                "vacuum": None,
                "fix_layers": None,
            }
        ],
    )
    assert len(scene.atoms) == 12  # 2*2*3 built despite the nulls
    assert sum(1 for a in scene.atoms.values() if a.fixed) == 0  # null => none frozen


def test_op_slab_bad_numeric_param_raises_clean_operror() -> None:
    # A non-coercible numeric (a null buried in size, a string vacuum) must raise
    # OpError — retryable — not a raw TypeError/ValueError that crashes apply_ops.
    pytest.importorskip("ase.build")
    from precis.structure import OpError

    scene = Scene(cell=_cubic(1.0))
    with pytest.raises(OpError):
        apply_ops(scene, [{"op": "slab", "element": "Pd", "size": [3, 3, None]}])
    with pytest.raises(OpError):
        apply_ops(
            scene,
            [{"op": "slab", "element": "Pd", "size": [2, 2, 2], "vacuum": "lots"}],
        )


def test_op_slab_fix_layers_as_list_gets_count_hint() -> None:
    # deepseek reads fix_layers as a list of indices; the error must name the
    # count-not-list semantics so an agent can self-correct.
    pytest.importorskip("ase.build")
    from precis.structure import OpError

    scene = Scene(cell=_cubic(1.0))
    with pytest.raises(OpError) as exc:
        apply_ops(
            scene,
            [{"op": "slab", "element": "Pd", "size": [3, 3, 3], "fix_layers": [0]}],
        )
    assert "count" in str(exc.value).lower()


def test_slab_extxyz_carries_fixatoms_for_autocatpath() -> None:
    """constraints=True serialises the frozen layers as a FixAtoms that ASE's
    own reader (autocatpath's slab hydrator) round-trips exactly."""
    pytest.importorskip("ase.build")
    import io as _io

    from ase import Atoms
    from ase.constraints import FixAtoms
    from ase.io import read as ase_read

    scene = Scene(cell=_cubic(1.0))
    apply_ops(
        scene,
        [{"op": "slab", "element": "Pd", "size": [2, 2, 3], "fix_layers": 1}],
    )
    xyz = export.to_extxyz(scene, constraints=True)
    atoms = ase_read(_io.StringIO(xyz), format="extxyz", index=0)
    assert isinstance(atoms, Atoms)  # index=0 → a single frame, not a list
    cons = [c for c in atoms.constraints if isinstance(c, FixAtoms)]
    assert cons and len(cons[0].get_indices()) == 4  # bottom layer stays fixed
    # the default (constraint-free) form keeps our label column and no FixAtoms
    plain = export.to_extxyz(scene)
    assert "label" in plain
