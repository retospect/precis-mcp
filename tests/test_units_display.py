"""Echo-on-write, TOON column units, prefunits, respond-in-kind —
the OPEN rounds of docs/backlog/pose-vector-units.md:

- ``understood:`` notes threaded from op quantity ingests to the handler
  (``op_notes_scope`` / ``apply_ops(notes=)``).
- ``format_quantity_column``: one common header unit per homogeneous
  column, per-value units when none fits.
- ``prefunits`` (per-call scope + sticky ``meta.prefunits``) and the
  respond-in-kind ``used`` units recorder.
"""

from __future__ import annotations

import json
import math
import re

import pytest

from precis.blocktree.ops import CORE_OPS
from precis.blocktree.ops import apply_ops as blocktree_apply_ops
from precis.design import states as design_states
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.cad import CadHandler
from precis.pcb.ir import _offset_rot
from precis.store import Store
from precis.utils.units import (
    capture_used_units,
    format_quantity,
    format_quantity_column,
    format_vector,
    op_notes_scope,
    parse_prefunits,
    parse_quantity,
    prefunits_scope,
    sticky_prefunits_scope,
)
from precis_se import persist
from precis_se.chain.vocab import length_m
from precis_se.handler import SeHandler, _block_line
from precis_se.ops import SeTree, apply_ops

# ── echo on write ──────────────────────────────────────────────────────────


def test_pose_rot_echoes_canonical() -> None:
    tree = SeTree()
    notes: list[str] = []
    apply_ops(
        tree,
        [
            {
                "op": "add_block",
                "name": "a",
                "pose": "[3 0 0] nm",
                "rot": "[0 0 90] deg",
            }
        ],
        notes=notes,
    )
    assert notes == [
        "understood: pose = [3 0 0] nm",
        "understood: rot = [0 0 90] deg",
    ]


def test_echo_covers_port_and_set_pose() -> None:
    tree = SeTree()
    notes: list[str] = []
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "a", "pose": "[1 0 0] mm"},
            {
                "op": "add_port",
                "block": "a",
                "name": "p",
                "pose": "[0 0 2] mm",
                "rot": "[0 0 30] deg",
            },
            {"op": "set_pose", "block": "a", "rot": "[0 0 45] deg"},
        ],
        notes=notes,
    )
    assert "understood: pose = [1 0 0] mm" in notes
    assert "understood: add_port pose for a.p = [0 0 2] mm" in notes
    assert "understood: add_port rot for a.p = [0 0 30] deg" in notes
    assert "understood: rot = [0 0 45] deg" in notes


def test_no_echo_for_absent_or_unbound() -> None:
    tree = SeTree()
    notes: list[str] = []
    apply_ops(tree, [{"op": "add_block", "name": "a"}], notes=notes)
    assert notes == []
    # No sink bound: same ops run silently.
    apply_ops(SeTree(), [{"op": "add_block", "name": "a", "pose": "[1 0 0] m"}])


def test_chain_vocab_length_echoes() -> None:
    notes: list[str] = []
    with op_notes_scope(notes):
        got = length_m("5 nm", "max_seg_len", "layout_chain")
    assert got == pytest.approx(5e-9)
    assert notes == ["understood: layout_chain max_seg_len = 5 nm"]


def test_waypoints_echo_once_per_field() -> None:
    """An N-waypoint path acknowledges ONCE — a field-level line naming
    all the parsed points, not one line per component (echo-flood fix)."""
    notes: list[str] = []
    apply_ops(
        SeTree(),
        [
            {"op": "add_block", "name": "stem"},
            {
                "op": "declare_helix",
                "block": "stem",
                "n_units": 4,
                "path": {
                    "waypoints": [
                        ["1nm", "0nm", "0nm"],
                        ["2nm", "0nm", "0nm"],
                        ["3nm", "0nm", "0nm"],
                    ]
                },
            },
        ],
        notes=notes,
    )
    wp = [n for n in notes if "waypoints" in n]
    assert wp == [
        "understood: declare_helix waypoints = [1 0 0] nm, [2 0 0] nm, [3 0 0] nm"
    ]


def test_nested_apply_ops_inherits_sink() -> None:
    notes: list[str] = []
    with op_notes_scope(notes):
        # A second, notes-free apply inside the bound region still echoes.
        blocktree_apply_ops(
            SeTree(),
            [{"op": "add_block", "name": "a", "pose": "[1 0 0] m"}],
            CORE_OPS,
        )
    assert notes == ["understood: pose = [1 0 0] m"]


# ── format_quantity_column ─────────────────────────────────────────────────


def test_column_common_unit() -> None:
    unit, cells = format_quantity_column([3e-9, 5e-9, 2e-8], "length")
    assert unit == "nm"
    assert cells == ["3", "5", "20"]


def test_column_no_common_unit_falls_to_per_value() -> None:
    # 1 nm beside 1 km: no single unit keeps every mantissa readable.
    unit, cells = format_quantity_column([1e-9, 1e3], "length")
    assert unit is None
    assert cells == ["1 nm", "1 km"]


def test_column_prefunit_wins() -> None:
    unit, cells = format_quantity_column(
        [1e-9, 1e-3], "length", prefunits={"length": "m"}
    )
    assert unit == "m"
    assert cells == ["1e-09", "0.001"]


def test_column_none_and_zero_cells() -> None:
    unit, cells = format_quantity_column([None, 0.0, 2.5e-3], "length")
    assert unit == "mm"
    assert cells == ["", "0", "2.5"]


def test_column_angle_is_degrees() -> None:
    unit, cells = format_quantity_column([math.pi / 2, math.pi], "angle")
    assert unit == "deg"
    assert cells == ["90", "180"]


# ── prefunits + respond-in-kind ────────────────────────────────────────────


def test_parse_prefunits_forms() -> None:
    assert parse_prefunits("nm,deg,kg") == {
        "length": "nm",
        "angle": "deg",
        "mass": "kg",
    }
    assert parse_prefunits(["mm", "rad"]) == {"length": "mm", "angle": "rad"}
    assert parse_prefunits({"length": "in"}) == {"length": "in"}
    assert parse_prefunits(None) == {}
    assert parse_prefunits("") == {}


def test_parse_prefunits_rejects() -> None:
    with pytest.raises(BadInput, match="unknown unit"):
        parse_prefunits("frobnicate")
    with pytest.raises(BadInput, match="not a plain unit"):
        parse_prefunits("3 nm")
    with pytest.raises(BadInput, match="not a length unit"):
        parse_prefunits({"length": "deg"})
    with pytest.raises(BadInput, match="unknown dimension"):
        parse_prefunits({"time": "s"})


def test_prefunits_scope_renders_in_unit() -> None:
    with prefunits_scope({"length": "mm"}):
        assert format_quantity(3e-9, "length") == "3e-06 mm"
        assert format_vector([3e-9, 0, 0], "length") == "[3e-06 0 0] mm"
        assert format_quantity(math.pi / 2, "angle") == "90°"
    assert format_quantity(3e-9, "length") == "3 nm"


def test_used_units_record_and_conflict() -> None:
    used: dict = {}
    with capture_used_units(used):
        parse_quantity("3 mm", "length")
        parse_quantity("4 mm", "length")
    assert used == {"length": "mm"}
    with capture_used_units(used := {}):
        parse_quantity("3 mm", "length")
        parse_quantity("4 in", "length")
    assert used == {"length": None}  # conflicting → neutral


def test_precedence_call_over_used_over_sticky() -> None:
    used: dict = {}
    with capture_used_units(used), sticky_prefunits_scope({"length": "in"}):
        parse_quantity("3 mm", "length")
        # used (mm) beats sticky (in)
        assert format_quantity(3e-3, "length") == "3 mm"
        with prefunits_scope({"length": "cm"}):
            # per-call beats both
            assert format_quantity(3e-3, "length") == "0.3 cm"
        # explicit arg beats everything
        assert format_quantity(3e-3, "length", prefunits={"length": "m"}) == "0.003 m"


def test_rad_pref_on_angle() -> None:
    with prefunits_scope({"angle": "rad"}):
        assert format_quantity(math.pi / 2, "angle") == "1.5708 rad"
        assert format_vector([0, 0, math.pi / 2], "angle") == "[0 0 1.571] rad"


def test_bad_pref_falls_back_with_note() -> None:
    """A display preference that cannot convert must not fail the render
    (a committed write never dies on display) — it falls back to the
    canonical unit and leaves a note on the op-notes sink."""
    notes: list[str] = []
    with op_notes_scope(notes):
        got = format_quantity(3e-3, "length", prefunits={"length": "deg"})
        vec = format_vector([3e-3, 0, 0], "length", prefunits={"length": "deg"})
    assert got == "3 mm"
    assert vec == "[3 0 0] mm"
    assert any("not a length unit" in n for n in notes)


# ── tree-view units ────────────────────────────────────────────────────────


def test_block_line_shows_units() -> None:
    tree = apply_ops(
        SeTree(),
        [
            {
                "op": "add_block",
                "name": "a",
                "pose": "[3 0 0] mm",
                "rot": "[0 0 90] deg",
            }
        ],
    )
    line = _block_line(tree, tree.blocks["a"])
    assert "pose=[3 0 0] mm" in line
    assert "rot=[0 0 90] deg" in line


# ── pcb group_offset.rot ───────────────────────────────────────────────────


def test_group_offset_rot_unit_forms() -> None:
    assert _offset_rot(90) == 90.0
    assert _offset_rot("90deg") == 90.0
    assert _offset_rot("90") == 90.0
    assert _offset_rot("1.5708 rad") == pytest.approx(90.0, rel=1e-3)
    # Malformed input still degrades to 0, never raises.
    assert _offset_rot("junk") == 0.0
    assert _offset_rot(None) == 0.0


# ── end to end (store-backed) ──────────────────────────────────────────────


@pytest.fixture
def se(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


@pytest.fixture
def cad(store: Store) -> CadHandler:
    return CadHandler(hub=Hub(store=store))


def test_se_put_echoes_and_tree_shows_units(se: SeHandler) -> None:
    resp = se.put(
        id="d1",
        text=json.dumps(
            {"ops": [{"op": "add_block", "name": "a", "pose": "[3 0 0] nm"}]}
        ),
    )
    assert "understood: pose = [3 0 0] nm" in resp.body
    assert "pose=[3 0 0] nm" in resp.body


def test_se_sticky_and_per_call_prefunits(se: SeHandler) -> None:
    se.put(
        id="d2",
        text=json.dumps(
            {
                "prefunits": "mm",
                "ops": [{"op": "add_block", "name": "a", "pose": "[3 0 0] mm"}],
            }
        ),
    )
    # Sticky: a later get renders in mm even though args carry no pref.
    body = se.get(id="d2").body
    assert "pose=[3 0 0] mm" in body
    # Per-call override beats the sticky pref.
    body = se.get(id="d2", args={"prefunits": "m"}).body
    assert "pose=[0.003 0 0] m" in body


def test_se_edit_prefunits_is_a_meta_write(se: SeHandler) -> None:
    se.put(
        id="d3",
        text=json.dumps(
            {"ops": [{"op": "add_block", "name": "a", "pose": "[3 0 0] nm"}]}
        ),
    )
    se.edit(id="d3", args={"prefunits": "um"})
    ref = se.store.get_ref(kind="se", id="d3")
    assert ref is not None
    assert (ref.meta or {}).get("prefunits") == {"length": "µm"}
    body = se.get(id="d3").body
    assert "pose=[0.003 0 0] µm" in body


def test_cad_prefunits_directive_and_per_call_override(cad: CadHandler) -> None:
    cad.put(
        id="flange",
        text="prefunits: l\nplate add cyl:r25mmh8mm",
    )
    resp = cad.get(id="flange", view="volume")
    # Sticky litre: ~15.7 ml shows as ~0.0157 l.
    assert "volume: 0.015" in resp.body and " l" in resp.body
    # Per-call wins over the DSL-declared sticky pref.
    resp = cad.get(id="flange", view="volume", args={"prefunits": "ml"})
    assert " ml" in resp.body and " l" not in resp.body.split(" ml")[0]


def test_cad_respond_in_kind_from_args(cad: CadHandler) -> None:
    cad.put(id="flange", text="plate add cyl:r25mmh8mm")
    resp = cad.get(id="flange", view="point", args={"p": ["0mm", "0mm", "4mm"]})
    # The caller's own unit (mm) becomes the column's header unit.
    assert "measure (mm)" in resp.body


def test_se_prefunits_conflicts_with_restore_and_dryrun(se: SeHandler) -> None:
    se.put(
        id="d4",
        text=json.dumps(
            {"ops": [{"op": "add_block", "name": "a", "pose": "[1 0 0] m"}]}
        ),
    )
    with pytest.raises(BadInput, match="cannot ride an op list"):
        se.edit(
            id="d4",
            text=json.dumps(
                {
                    "prefunits": "mm",
                    "ops": [{"op": "restore_revision", "rev": 1}],
                }
            ),
        )


def test_sweep_gap_column_shows_mantissa_under_header_unit(
    se: SeHandler, store: Store
) -> None:
    """A sweep hit's gap cell is the mantissa under ``gap (<unit>)`` — not
    the raw SI float (the pop-vs-cell slip that put -0.0008 under a
    ``gap (mm)`` header). Recipe from tests/test_se_joint_sweep.py."""
    ops = [
        {
            "op": "add_block",
            "name": "frame_box",
            "envelope": "box:w0.01d0.01h0.01",
            "pose": "[0 0 -0.012] m",
        },
        {"op": "add_port", "block": "frame_box", "name": "q", "pose": "[0 0 0.012] m"},
        {
            "op": "add_block",
            "name": "arm",
            "pose": "[0.03 0.0 0.0] m",
            "envelope": "box:w0.04d0.01h0.01",
        },
        {"op": "add_port", "block": "arm", "name": "p", "pose": "[-0.03 0 0] m"},
        {
            "op": "add_block",
            "name": "post",
            "envelope": "box:w0.006d0.006h0.01",
            "pose": "[0.0 0.03 0.0] m",
        },
        {
            "op": "connect",
            "a": "frame_box.q",
            "b": "arm.p",
            "joint": {"class": "revolute", "axis": [0.0, 0.0, 1.0]},
        },
        {
            "op": "declare_states",
            "block": "arm",
            "states": [{"name": "rest"}, {"name": "quarter"}],
        },
    ]
    se.put(id="sweepcol", text=json.dumps({"ops": ops}))
    ref = se.store.get_ref(kind="se", id="sweepcol")
    assert ref is not None
    uid = persist.load_tree(se.store, ref.id).blocks["arm"].uid
    assert uid is not None
    design_states.set_state_pose(
        se.store,
        ref.id,
        uid,
        "quarter",
        {"xyz": [0.0, 0.03, 0.0], "rot": [0.0, 0.0, math.pi / 2.0]},
    )
    body = se.get(id="sweepcol", view="sweep").body
    assert re.search(r"gap \(\w+\)", body), body
    line = next(ln for ln in body.splitlines() if "arm ↔ post" in ln)
    mantissa = float(line.rsplit("\t", 1)[-1])
    # The mantissa belongs in the column window; the SI float (-0.0008-ish
    # metres) would be ~1000× off under a mm header.
    assert 1e-3 <= abs(mantissa) < 1e4
