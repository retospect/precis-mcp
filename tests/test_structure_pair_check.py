"""``view='pair'`` — the NEB endpoint pair check (gr512126, slice 2).

The gate evaluation is a pure function over two hydrated scenes plus their
run history (``pair_check.evaluate_pair``); these tests exercise it on
in-memory scenes — the slice-1 acceptance pair (Cu(111)[3,3,4] + bridging
O + top-site CH4, FS derived by ``transfer`` + ``move_group``) built with
the same ops the MCP path uses — and the handler-level ``view='pair'``
render against the store fixture. Every spec gate gets a passing case and
one failing case whose ``next`` is asserted.
"""

from __future__ import annotations

import copy

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.structure import StructureHandler
from precis.structure import Scene, apply_ops
from precis.structure.cell import Cell
from precis.structure.pair_check import evaluate_pair
from tests.test_structure_endpoints import _TRANSFER_OP, _endpoint_is, _fs_ops


def _ml_run(model: str = "mace_mp", version: int = 1) -> dict:
    """A stubbed converged ml run row (the ``struct_runs`` shape)."""
    return {
        "fidelity": "ml",
        "status": "succeeded",
        "converged": True,
        "on_version": version,
        "model": model,
    }


def _pair_scenes(element: str = "Cu") -> tuple[Scene, Scene]:
    pytest.importorskip("ase")
    scene_is, mg_site, _spare = _endpoint_is(element)
    scene_fs = copy.deepcopy(scene_is)
    apply_ops(scene_fs, _fs_ops(mg_site))
    return scene_is, scene_fs


def _verdict(verdict, gate: str) -> str:
    return next(g.verdict for g in verdict.gates if g.gate == gate)


def _gate(verdict, gate: str):
    return next(g for g in verdict.gates if g.gate == gate)


# -- the happy path ---------------------------------------------------------


def test_slice1_pair_unrelaxed_fails_only_the_relaxed_gate() -> None:
    scene_is, scene_fs = _pair_scenes()
    verdict = evaluate_pair(
        scene_is,
        scene_fs,
        is_version=1,
        fs_version=1,
        is_slug="cu_is",
        fs_slug="cu_fs",
    )
    assert _verdict(verdict, "relaxed") == "fail"
    relaxed = _gate(verdict, "relaxed")
    # Both endpoints need a relax — both edits appear in the next=.
    assert "'cu_is'" in relaxed.next and "'cu_fs'" in relaxed.next
    assert "'op': 'relax'" in relaxed.next and "'fidelity': 'ml'" in relaxed.next
    for gate in verdict.gates:
        if gate.gate == "relaxed":
            continue
        assert gate.verdict == "pass", (gate.gate, gate.detail)


def test_slice1_pair_all_gates_pass_with_stubbed_ml_runs() -> None:
    scene_is, scene_fs = _pair_scenes()
    verdict = evaluate_pair(
        scene_is,
        scene_fs,
        is_version=1,
        fs_version=1,
        is_runs=[_ml_run()],
        fs_runs=[_ml_run()],
        is_slug="cu_is",
        fs_slug="cu_fs",
    )
    assert [g.verdict for g in verdict.gates] == ["pass"] * len(verdict.gates)
    assert verdict.ok


# -- same atoms -------------------------------------------------------------


def test_shuffled_labels_fail_same_atoms_with_derive_next() -> None:
    """A separately ``put`` FS gets fresh labels — never pairable."""
    scene_is, _scene_fs = _pair_scenes()
    # A lookalike FS built by re-putting the same ops gets aCu* labels —
    # simulate the label mismatch with a renamed copy.
    other = copy.deepcopy(scene_is)
    atom = other.atoms.pop("aO1")
    atom.label = "aO9"
    other.atoms["aO9"] = atom
    verdict = evaluate_pair(scene_is, other, is_slug="cu_is", fs_slug="cu_fs")
    g = _gate(verdict, "same atoms")
    assert g.verdict == "fail"
    assert "aO1" in g.detail and "aO9" in g.detail
    assert "derive_from" in g.next and "'cu_is'" in g.next
    # Geometry gates that need a shared label set go n/a, not garbage.
    assert _verdict(verdict, "same fixed set") == "n/a"
    assert _verdict(verdict, "distinct basins") == "n/a"
    assert _verdict(verdict, "one elementary step") == "n/a"


def test_element_or_charge_mismatch_fails_same_atoms() -> None:
    scene_is, scene_fs = _pair_scenes()
    scene_fs.atoms["aO1"].charge = -1
    verdict = evaluate_pair(scene_is, scene_fs)
    assert _verdict(verdict, "same atoms") == "fail"


# -- same cell ---------------------------------------------------------------


def test_different_cell_fails_same_cell_and_skips_geometry() -> None:
    scene_is, scene_fs = _pair_scenes()
    scene_fs.cell = Cell(scene_fs.cell.lattice * 1.01, scene_fs.cell.pbc)
    verdict = evaluate_pair(scene_is, scene_fs)
    assert _verdict(verdict, "same cell") == "fail"
    assert "derive_from" in _gate(verdict, "same cell").next
    assert _verdict(verdict, "distinct basins") == "n/a"
    assert _verdict(verdict, "no cross-cell drag") == "n/a"


# -- same fixed set ----------------------------------------------------------


def test_fixed_flag_mismatch_fails_with_constrain_next() -> None:
    scene_is, scene_fs = _pair_scenes()
    fixed = next(la for la, a in scene_is.atoms.items() if a.fixed)
    scene_fs.atoms[fixed].fixed = 0
    verdict = evaluate_pair(scene_is, scene_fs, fs_slug="cu_fs")
    g = _gate(verdict, "same fixed set")
    assert g.verdict == "fail"
    assert "'op': 'constrain'" in g.next and "'cu_fs'" in g.next


# -- slab anchored -----------------------------------------------------------


def test_free_slab_fails_slab_anchored() -> None:
    """st511725's shape: a slab with nothing fixed drifts under a relax."""
    scene_is, scene_fs = _pair_scenes()
    for scene in (scene_is, scene_fs):
        for atom in scene.atoms.values():
            atom.fixed = 0
    verdict = evaluate_pair(scene_is, scene_fs)
    g = _gate(verdict, "slab anchored")
    assert g.verdict == "fail"
    assert "constrain" in g.next


def test_molecule_pair_has_no_slab_to_anchor() -> None:
    scene_is = Scene(cell=Cell(np.eye(3) * 20.0, pbc=(False,) * 3))
    apply_ops(
        scene_is,
        [
            {"op": "add_atom", "element": "C", "cart": [10.0, 10.0, 10.0]},
            {"op": "add_atom", "element": "H", "cart": [11.09, 10.0, 10.0]},
            {"op": "add_atom", "element": "O", "cart": [10.0, 10.0, 12.0]},
            {"op": "add_bond", "i": "aC1", "j": "aH1", "order": 1},
        ],
    )
    scene_fs = copy.deepcopy(scene_is)
    apply_ops(scene_fs, [_TRANSFER_OP])
    verdict = evaluate_pair(scene_is, scene_fs)
    assert _verdict(verdict, "slab anchored") == "pass"


# -- valid -------------------------------------------------------------------


def test_invalid_endpoint_fails_valid_with_the_fix_text() -> None:
    """st510794's shape: an atom_overlap error names its own fix."""
    scene_is, scene_fs = _pair_scenes()
    overlap = scene_is.atoms["aO1"]
    overlap.frac = scene_is.atoms["aC1"].frac + np.array([0.01, 0.0, 0.0])
    verdict = evaluate_pair(scene_is, scene_fs)
    g = _gate(verdict, "valid")
    assert g.verdict == "fail"
    assert "error(s)" in g.detail
    # The validator's own suggested_fix text surfaces as the next=.
    assert "hard-sphere floor" in g.next or "aO1" in g.next


# -- relaxed -----------------------------------------------------------------


def test_relaxed_fails_on_wrong_version_and_model_mismatch() -> None:
    scene_is, scene_fs = _pair_scenes()
    verdict = evaluate_pair(
        scene_is,
        scene_fs,
        is_version=2,
        fs_version=1,
        is_runs=[_ml_run(version=1)],  # stale: on the old version
        fs_runs=[_ml_run()],
    )
    assert _verdict(verdict, "relaxed") == "fail"
    verdict = evaluate_pair(
        scene_is,
        scene_fs,
        is_version=1,
        fs_version=1,
        is_runs=[_ml_run(model="chgnet")],
        fs_runs=[_ml_run(model="mace_mp")],
    )
    g = _gate(verdict, "relaxed")
    assert g.verdict == "fail"
    assert "'model': 'chgnet'" in g.next  # re-relax the FS at the IS's model


def test_unconverged_ml_run_does_not_count() -> None:
    scene_is, scene_fs = _pair_scenes()
    run = _ml_run()
    run["converged"] = False
    verdict = evaluate_pair(scene_is, scene_fs, is_runs=[run], fs_runs=[_ml_run()])
    assert _verdict(verdict, "relaxed") == "fail"


# -- distinct basins / cross-cell drag ---------------------------------------


def test_fs_equals_is_fails_distinct_basins() -> None:
    scene_is, _scene_fs = _pair_scenes()
    scene_fs = copy.deepcopy(scene_is)  # a derive with no ops
    verdict = evaluate_pair(
        scene_is,
        scene_fs,
        is_runs=[_ml_run()],
        fs_runs=[_ml_run()],
    )
    g = _gate(verdict, "distinct basins")
    assert g.verdict == "fail"
    assert "identical geometry" in g.detail
    # And with no bond change either, the step gate agrees.
    assert _verdict(verdict, "one elementary step") == "fail"


def test_sub_threshold_move_fails_distinct_basins() -> None:
    scene_is, _ = _pair_scenes()
    # A jiggled copy of the IS: geometry differs, but nothing moved ≥0.3 Å.
    scene_fs = copy.deepcopy(scene_is)
    rng = np.random.default_rng(0)
    for atom in scene_fs.atoms.values():
        if not atom.fixed:
            atom.frac = scene_fs.cell.wrap(atom.frac + rng.normal(0, 0.002, 3) / 20.0)
    verdict = evaluate_pair(scene_is, scene_fs)
    g = _gate(verdict, "distinct basins")
    assert g.verdict == "fail"
    assert "max adsorbate move" in g.detail


def test_cross_cell_drag_warns_over_five_angstrom() -> None:
    scene_is, scene_fs = _pair_scenes()
    # Park a non-fixed adsorbate atom ~8 Å away in the FS.
    h = scene_fs.atoms["aH4"]
    h.frac = scene_fs.cell.wrap(h.frac + np.array([0.0, 0.0, 0.4]))
    verdict = evaluate_pair(scene_is, scene_fs)
    g = _gate(verdict, "no cross-cell drag")
    assert g.verdict == "warn"
    assert "aH4" in g.detail and g.next


# -- one elementary step -----------------------------------------------------


def test_multi_bond_change_warns_as_not_elementary() -> None:
    scene_is, scene_fs = _pair_scenes()
    apply_ops(
        scene_fs,
        [
            {"op": "add_bond", "i": "aO1", "j": "aH1", "order": 1},
            {"op": "remove_bond", "i": "aC1", "j": "aH1"},
            {"op": "add_bond", "i": "aO1", "j": "aH2", "order": 1},
            {"op": "remove_bond", "i": "aC1", "j": "aH2"},
        ],
    )
    verdict = evaluate_pair(scene_is, scene_fs)
    g = _gate(verdict, "one elementary step")
    assert g.verdict == "warn"
    assert "split into two steps" in g.next


# -- handler: the view='pair' render ----------------------------------------


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


_IS_PAYLOAD = (
    '{"cell": {"a": 20.0, "b": 20.0, "c": 20.0, "pbc": [false, false, false]},'
    ' "ops": ['
    '  {"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]},'
    '  {"op": "add_atom", "element": "H", "frac": [0.5545, 0.5, 0.5]},'
    '  {"op": "add_bond", "i": "aC1", "j": "aH1", "order": 1},'
    '  {"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.65]}'
    "]}"
)


def _put_is_fs(structure) -> None:
    structure.put(id="ch_is", text=_IS_PAYLOAD)
    structure.put(
        id="ch_fs",
        args={"derive_from": "ch_is", "ops": [dict(_TRANSFER_OP)]},
    )


def test_view_pair_renders_the_gate_table(structure) -> None:
    _put_is_fs(structure)
    resp = structure.get(id="ch_is", view="pair", args={"other": "ch_fs"})
    body = resp.body
    for gate in (
        "same atoms",
        "same cell",
        "same fixed set",
        "slab anchored",
        "valid",
        "relaxed",
        "distinct basins",
        "one elementary step",
        "no cross-cell drag",
    ):
        assert gate in body
    # No runs yet: the relaxed gate fails and names both relax edits.
    assert "fail" in body
    assert "'ch_is'" in body and "'ch_fs'" in body


def test_view_pair_all_pass_after_converged_ml_runs(structure, store) -> None:
    _put_is_fs(structure)
    for slug in ("ch_is", "ch_fs"):
        ref = store.get_ref(kind="structure", id=slug)
        store.structure_record_run(
            ref.id,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=120,
            max_disp=0.0,
            model="mace_mp",
            energy=-12.3,
        )
    resp = structure.get(id="ch_is", view="pair", args={"other": "ch_fs"})
    assert "fail" not in resp.body
    assert "NEB-ready" in resp.body


def test_view_pair_fs_is_same_design_fails_basins(structure) -> None:
    structure.put(id="ch_is", text=_IS_PAYLOAD)
    resp = structure.get(id="ch_is", view="pair", args={"other": "ch_is"})
    assert "distinct basins" in resp.body
    assert "identical geometry" in resp.body


def test_view_pair_refuses_without_other_and_on_unknown_slug(structure) -> None:
    structure.put(id="ch_is", text=_IS_PAYLOAD)
    with pytest.raises(BadInput) as exc:
        structure.get(id="ch_is", view="pair")
    assert exc.value.next and "pair" in exc.value.next
    with pytest.raises(NotFound):
        structure.get(id="ch_is", view="pair", args={"other": "no_such_fs"})
