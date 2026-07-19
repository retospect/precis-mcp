"""Unit tests for the pure ``structure`` IR kernel (ADR 0043 increment 1).

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


# -- export (pure formats) ---------------------------------------------------


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

    def _no_mlip(model):  # type: ignore[no-untyped-def]
        raise RelaxUnsupported("no MLIP backend (test)")

    monkeypatch.setattr(relax_mod, "_ml_calculator", _no_mlip)
    scene = Scene(cell=_cubic(20.0))
    apply_ops(scene, [{"op": "add_atom", "element": "Pd", "frac": [0, 0, 0]}])
    for rung in ("ml", "dft-fast", "xtb"):
        with pytest.raises(RelaxUnsupported):
            relax(scene, fidelity=rung)


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
    bottom `fix_layers` layers (mirrors catpath's build_slab so the geometry can
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


def test_slab_extxyz_carries_fixatoms_for_catpath() -> None:
    """constraints=True serialises the frozen layers as a FixAtoms that ASE's
    own reader (catpath's slab hydrator) round-trips exactly."""
    pytest.importorskip("ase.build")
    import io as _io

    from ase.constraints import FixAtoms
    from ase.io import read as ase_read

    scene = Scene(cell=_cubic(1.0))
    apply_ops(
        scene,
        [{"op": "slab", "element": "Pd", "size": [2, 2, 3], "fix_layers": 1}],
    )
    xyz = export.to_extxyz(scene, constraints=True)
    atoms = ase_read(_io.StringIO(xyz), format="extxyz", index=0)
    cons = [c for c in atoms.constraints if isinstance(c, FixAtoms)]
    assert cons and len(cons[0].get_indices()) == 4  # bottom layer stays fixed
    # the default (constraint-free) form keeps our label column and no FixAtoms
    plain = export.to_extxyz(scene)
    assert "label" in plain
