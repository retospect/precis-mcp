"""Dogfood 2026-09-28 residuals (gr454488 + gr454563): the fixes a naive
first-time user's round asked for, pinned one by one.

- generate states ``mode='atomic'`` on the block it mints (no
  ``mode_binding_mismatch`` the tool itself created);
- generator-declared length anchors carry ``origin='generated'``;
- the check-mode echo is headed "check", not "dry-run";
- the persisted stick record carries the geometry tier;
- a flat sheet's ``rim`` port direction is the frame axis, not centroid
  jitter;
- ``strength='hard'`` is consumed by the stack-up (mismatch → error) and
  no longer draws ``minimum_constraint`` on a toleranced relation;
- an ``envelope_fit`` protrusion on a legacy generated block says
  "regenerate", not "widen the envelope".
"""

from __future__ import annotations

import numpy as np

from precis.store import Store
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis_se.atomic import validate as se_atomic_validate
from precis_se.atomic.generate import prepare_generate
from precis_se.atomic.generators import GENERATORS
from precis_se.drc import drc
from precis_se.ops import SeBlock, SeTree, apply_ops

TUBE_SPEC = """\
hexfold 0.1

lattice: element=C sigma=1.42

origin post
post: tube(5,5,len=4)
"""

SHEET_A_SPEC = """\
hexfold 0.2

lattice: element=C sigma=1.42

s: sheet(25A, 12)
"""


def _generate(store: Store, tree: SeTree, spec: str, name: str, **params: object):
    return prepare_generate(
        store,
        tree,
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": spec, **params},
            "name": name,
        },
        "hx-design",
    )


def test_generate_sets_atomic_mode_so_drc_has_no_self_inflicted_mismatch(
    store: Store,
) -> None:
    tree = SeTree()
    _echo, pending = _generate(store, tree, TUBE_SPEC, "tube")
    assert pending is not None
    assert tree.blocks["tube"].mode == "atomic"
    # The block is bound to its structure only after finish_generate; set
    # the binding the way finish_generate does and run DRC over it.
    tree.blocks["tube"].bound_kind = "structure"
    tree.blocks["tube"].bound = pending.struct_slug
    rules = {f.rule for f in drc(tree).findings if f.subject == "tube"}
    assert "mode_binding_mismatch" not in rules


def test_generated_length_anchors_are_stamped_generated(store: Store) -> None:
    tree = SeTree()
    _echo, pending = _generate(store, tree, SHEET_A_SPEC, "sheet")
    assert pending is not None
    origins = {m.name: m.origin for m in tree.measures if m.block == "sheet"}
    assert origins == {"s_W": "generated", "s_H": "generated"}


def test_check_mode_echo_is_headed_check_not_dry_run(store: Store) -> None:
    tree = SeTree()
    echo, pending = _generate(store, tree, TUBE_SPEC, "probe", fidelity="check")
    assert pending is None
    assert echo.startswith("hexfold check for block 'probe':")
    assert "dry-run" not in echo


def test_stick_record_carries_the_geometry_tier() -> None:
    block = GENERATORS["hexfold"]({"spec": TUBE_SPEC})
    codes = {f["code"] for f in block.topology["report"]["findings"]}
    assert "geom.summary" in codes
    # …and it is the same tier the check echo shows.
    echo = GENERATORS["hexfold"]({"spec": TUBE_SPEC, "fidelity": "check"})
    assert "geom.summary" in echo.provenance


def test_flat_sheet_rim_direction_is_the_frame_axis_not_jitter() -> None:
    sheet = GENERATORS["hexfold"]({"spec": SHEET_A_SPEC})
    (rim,) = sheet.ports
    assert rim.direction == [0.0, 0.0, 1.0]
    # A tube's two rims still point away from the body, in opposite
    # directions along the frame axis.
    tube = GENERATORS["hexfold"]({"spec": TUBE_SPEC})
    dirs = {p.name: np.asarray(p.direction) for p in tube.ports}
    assert set(dirs) == {"in", "out"}
    assert float(dirs["in"] @ dirs["out"]) < -0.99
    assert abs(float(dirs["out"][2])) > 0.99


def _stackup_tree(strength: str, declared: float) -> SeTree:
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "hub", "envelope": "box:w0.02d0.02h0.005"},
            {"op": "add_block", "name": "wheel", "envelope": "box:w0.03d0.03h0.005"},
            {"op": "add_measure", "block": "hub", "name": "od_d", "value": 0.016},
            {
                "op": "add_measure",
                "block": "wheel",
                "name": "bore_d",
                "value": declared,
                "relation": {"source": "hub.od_d", "offset": 0.0002, "tol": 5e-5},
                "strength": strength,
            },
        ],
    )
    return tree


def test_hard_with_a_toleranced_relation_draws_no_minimum_constraint() -> None:
    report = drc(_stackup_tree("hard", 0.0162))
    assert not [f for f in report.findings if f.rule == "minimum_constraint"]


def test_hard_turns_a_stackup_mismatch_into_an_error_gauge_keeps_a_warning() -> None:
    hard = drc(_stackup_tree("hard", 0.0170))
    (finding,) = [f for f in hard.findings if f.rule == "tolerance_mismatch"]
    assert finding.severity == "error"
    assert finding.subject == "wheel.bore_d"
    # The status line is humanized like the measures table cells.
    assert "17 mm" in finding.detail and "16.2 mm" in finding.detail
    assert "50 µm" in finding.detail
    gauge = drc(_stackup_tree("gauge", 0.0170))
    (finding,) = [f for f in gauge.findings if f.rule == "tolerance_mismatch"]
    assert finding.severity == "warn"


def _protruding_scene() -> Scene:
    cell = Cell.from_lengths_angles(30.0, 30.0, 30.0, pbc=(False, False, False))
    scene = Scene(cell=cell)
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    far = scene.cell.cart_to_frac(np.array([10.0, 0.0, 0.0]))
    scene.atoms["b"] = Atom(label="b", element="C", frac=scene.cell.wrap(far))
    return scene


def _hexfold_block(bound: str) -> SeTree:
    tree = SeTree()
    tree.blocks["blk"] = SeBlock(
        name="blk",
        envelope="sphere:r2e-10",
        descr="hexfold 0.2 spec (80 atoms, 110 bonds; rings {6: 40})",
        bound_kind="structure",
        bound=bound,
    )
    return tree


def test_legacy_generated_block_protrusion_says_regenerate() -> None:
    tree = _hexfold_block("legacy")
    findings = se_atomic_validate.validate_atomic(
        tree, bound_full_scenes={"legacy": _protruding_scene()}
    )
    (fit,) = [f for f in findings if f.rule == "envelope_fit"]
    assert "framing fix" in fit.detail and "generate again" in fit.detail
    assert "widen the envelope or rebind" not in fit.detail


def test_framed_generated_block_protrusion_is_a_genuine_drift() -> None:
    tree = _hexfold_block("framed")
    findings = se_atomic_validate.validate_atomic(
        tree,
        bound_full_scenes={"framed": _protruding_scene()},
        generated_bound=frozenset({"framed"}),
    )
    (fit,) = [f for f in findings if f.rule == "envelope_fit"]
    assert "widen the envelope or rebind" in fit.detail
    assert "framing fix" not in fit.detail
