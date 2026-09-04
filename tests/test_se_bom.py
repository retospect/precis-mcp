"""precis_se off-the-shelf rung 1 — bought items, modes, bindings
(docs/backlog/se-off-the-shelf-fabrication.md "Ship order" 1).

The load-bearing behaviour under test is the **multiplicity rollup**: one
authored BOM line is many bought things once the tree's arrays and
instances expand, and the number a purchase order needs is that product,
not the authored quantity. Everything else here is the vocabulary around
it (ops, vacancy rules, the mode/binding demands DRC can now check).

Same fixture shape as ``test_se_plugin.py``: the shared test DB template
carries only core migrations, so the plugin's own are seeded directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import precis_se
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.component import ComponentHandler
from precis.store import Store
from precis_se import bom as se_bom
from precis_se import drc as se_drc
from precis_se import persist
from precis_se import validate as se_validate
from precis_se.bom import BomLine
from precis_se.handler import SeHandler
from precis_se.ops import OpError, SeTree, apply_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


def _tree(ops: list[Any]) -> SeTree:
    return apply_ops(SeTree(), ops)


#: A wheel template placed once, plus a 4-up array of it — so the wheel's
#: *design* is realized five times (see the module docstring of
#: ``precis_se.bom``: a template is itself a placed block).
_WHEELS = [
    {"op": "add_block", "name": "cart"},
    {
        "op": "add_block",
        "name": "wheel",
        "parent": "cart",
        "envelope": "cyl:r0.05h0.02",
    },
    {
        "op": "array_block",
        "name": "wheels",
        "template": "wheel",
        "parent": "cart",
        "linear": {"count": 4, "pitch": 0.3, "axis": [1, 0, 0]},
    },
]


# ── the multiplicity rollup ─────────────────────────────────────────────


def test_bom_on_an_array_node_multiplies_by_its_count() -> None:
    tree = _tree(
        [
            *_WHEELS,
            {
                "op": "add_bom",
                "block": "wheels",
                "item_kind": "component",
                "item": "bearing-608",
                "qty": 2,
            },
        ]
    )
    (total,) = se_bom.rollup(tree)
    assert total.item == "bearing-608"
    assert total.total == pytest.approx(8.0)  # 2 per member × 4 members


def test_a_templates_realizations_include_its_instances() -> None:
    tree = _tree(_WHEELS)
    occ = se_bom.design_occurrences(tree)
    # the template's own placement (1) + the array's members (4)
    assert occ["wheel"] == 5
    assert occ["wheels"] == 4
    assert occ["cart"] == 1


def test_nested_arrays_multiply_through() -> None:
    """6 holes per panel × (1 template panel + 3 arrayed) = 24 rivets."""
    tree = _tree(
        [
            {"op": "add_block", "name": "panel", "envelope": "box:w0.3d0.2h0.003"},
            {"op": "add_block", "name": "hole", "parent": "panel"},
            {
                "op": "array_block",
                "name": "holes",
                "template": "hole",
                "parent": "panel",
                "polar": {"count": 6, "radius": 0.08},
            },
            {
                "op": "array_block",
                "name": "panels",
                "template": "panel",
                "linear": {"count": 3, "pitch": 0.25, "axis": [0, 1, 0]},
            },
            {
                "op": "add_bom",
                "block": "holes",
                "item_kind": "component",
                "item": "rivet-3x8",
            },
        ]
    )
    (total,) = se_bom.rollup(tree)
    assert total.total == pytest.approx(24.0)


def test_connect_line_takes_the_larger_endpoint_count() -> None:
    """Four wheels on one axle need four sets of bearings — the arrayed
    side drives a connect's multiplicity, and the choice is stated."""
    tree = _tree(
        [
            *_WHEELS,
            {"op": "add_block", "name": "axle", "parent": "cart"},
            {"op": "add_port", "block": "axle", "name": "shaft"},
            {"op": "add_port", "block": "wheel", "name": "bore"},
            {"op": "connect", "a": "axle.shaft", "b": "wheels.bore"},
            {
                "op": "add_bom",
                "a": "axle.shaft",
                "b": "wheels.bore",
                "item_kind": "component",
                "item": "bearing-608",
                "qty": 2,
            },
        ]
    )
    (line,) = tree.bom
    occurrences, note = se_bom.line_occurrences(tree, line)
    assert occurrences == 4
    assert note is not None
    assert "axle" in note and "wheels" in note
    (total,) = se_bom.rollup(tree)
    assert total.total == pytest.approx(8.0)


def test_unresolved_target_is_excluded_from_totals_and_reported() -> None:
    tree = _tree(
        [
            *_WHEELS,
            {
                "op": "add_bom",
                "block": "wheels",
                "item_kind": "component",
                "item": "bearing-608",
                "qty": 2,
            },
        ]
    )
    # Hand-corrupted row: ops' vacancy rules never leave one of these, so
    # this is the defense-in-depth path (validate must still catch it).
    tree.bom.append(
        BomLine(item_kind="component", item="bearing-608", qty=99, block="ghost")
    )
    (total,) = se_bom.rollup(tree)
    assert total.total == pytest.approx(8.0)  # the ghost line contributes nothing
    assert total.unresolved == 1
    rules = {f.rule for f in se_validate.validate(tree)}
    assert "dangling_bom" in rules


def test_two_lines_in_different_units_are_flagged_not_silently_summed() -> None:
    """0.4 m of tube plus 2 each of tube is not 2.4 of anything — the
    rollup keeps both units so the view can say so."""
    tree = _tree(
        [
            *_WHEELS,
            {
                "op": "add_bom",
                "block": "wheel",
                "item_kind": "component",
                "item": "alu-tube-20",
                "qty": 0.4,
                "uom": "m",
            },
            {
                "op": "add_bom",
                "block": "cart",
                "item_kind": "component",
                "item": "alu-tube-20",
                "qty": 2,
                "uom": "each",
            },
        ]
    )
    (total,) = se_bom.rollup(tree)
    assert total.mixed_uom
    assert total.uoms == {"m", "each"}


# ── ops ─────────────────────────────────────────────────────────────────


def test_repeat_add_bom_replaces_the_line() -> None:
    tree = _tree(
        [
            *_WHEELS,
            {
                "op": "add_bom",
                "block": "wheel",
                "item_kind": "component",
                "item": "bearing-608",
                "qty": 2,
            },
            {
                "op": "add_bom",
                "block": "wheel",
                "item_kind": "component",
                "item": "bearing-608",
                "qty": 4,
            },
        ]
    )
    assert len(tree.bom) == 1
    assert tree.bom[0].qty == pytest.approx(4.0)


def test_remove_bom_drops_one_line() -> None:
    tree = _tree(
        [
            *_WHEELS,
            {
                "op": "add_bom",
                "block": "wheel",
                "item_kind": "component",
                "item": "bearing-608",
            },
            {
                "op": "remove_bom",
                "block": "wheel",
                "item_kind": "component",
                "item": "bearing-608",
            },
        ]
    )
    assert tree.bom == []


def test_remove_bom_unknown_item_lists_what_is_there() -> None:
    with pytest.raises(OpError) as exc:
        _tree(
            [
                *_WHEELS,
                {
                    "op": "add_bom",
                    "block": "wheel",
                    "item_kind": "component",
                    "item": "bearing-608",
                },
                {
                    "op": "remove_bom",
                    "block": "wheel",
                    "item_kind": "component",
                    "item": "bearing-609",
                },
            ]
        )
    assert "bearing-608" in str(exc.value)


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        ({"op": "add_bom", "block": "wheel", "item": "x"}, "item_kind"),
        (
            {
                "op": "add_bom",
                "block": "wheel",
                "item_kind": "widget",
                "item": "x",
            },
            "item_kind",
        ),
        (
            {"op": "add_bom", "block": "wheel", "item_kind": "component"},
            "needs 'item'",
        ),
        (
            {
                "op": "add_bom",
                "block": "wheel",
                "item_kind": "component",
                "item": "x",
                "qty": 0,
            },
            "> 0",
        ),
        (
            {"op": "add_bom", "item_kind": "component", "item": "x"},
            "exactly one",
        ),
        (
            {
                "op": "add_bom",
                "block": "wheel",
                "a": "axle.shaft",
                "item_kind": "component",
                "item": "x",
            },
            "exactly one",
        ),
        (
            {
                "op": "add_bom",
                "block": "ghost",
                "item_kind": "component",
                "item": "x",
            },
            "ghost",
        ),
    ],
)
def test_add_bom_rejections(op: dict[str, Any], expected: str) -> None:
    with pytest.raises(OpError) as exc:
        _tree([*_WHEELS, op])
    assert expected in str(exc.value)


def test_removing_a_block_takes_its_bom_lines() -> None:
    tree = _tree(
        [
            *_WHEELS,
            {"op": "add_block", "name": "bracket", "parent": "cart"},
            {
                "op": "add_bom",
                "block": "bracket",
                "item_kind": "component",
                "item": "m6-bolt",
                "qty": 4,
            },
            {"op": "remove_block", "block": "bracket"},
        ]
    )
    assert tree.bom == []


def test_disconnecting_takes_the_joints_bom_lines() -> None:
    tree = _tree(
        [
            *_WHEELS,
            {"op": "add_block", "name": "axle", "parent": "cart"},
            {"op": "add_port", "block": "axle", "name": "shaft"},
            {"op": "add_port", "block": "wheel", "name": "bore"},
            {"op": "connect", "a": "axle.shaft", "b": "wheel.bore"},
            {
                "op": "add_bom",
                "a": "axle.shaft",
                "b": "wheel.bore",
                "item_kind": "component",
                "item": "bearing-608",
                "qty": 2,
            },
            {"op": "disconnect", "a": "axle.shaft", "b": "wheel.bore"},
        ]
    )
    assert tree.bom == []


# ── modes and bindings ──────────────────────────────────────────────────


def test_set_mode_accepts_a_known_family_and_clears() -> None:
    tree = _tree([*_WHEELS, {"op": "set_mode", "block": "wheel", "mode": "purchase"}])
    assert tree.blocks["wheel"].mode == "purchase"
    apply_ops(tree, [{"op": "set_mode", "block": "wheel", "mode": None}])
    assert tree.blocks["wheel"].mode is None


def test_set_mode_records_intent_for_an_unimplemented_family() -> None:
    tree = _tree([*_WHEELS, {"op": "set_mode", "block": "wheel", "mode": "fdm/asa"}])
    assert tree.blocks["wheel"].mode == "fdm/asa"


def test_set_mode_rejects_an_unknown_family_with_the_list() -> None:
    with pytest.raises(OpError) as exc:
        _tree([*_WHEELS, {"op": "set_mode", "block": "wheel", "mode": "wishing/hard"}])
    assert "purchase" in str(exc.value)


def test_set_mode_on_an_instance_points_at_the_template() -> None:
    with pytest.raises(OpError) as exc:
        _tree([*_WHEELS, {"op": "set_mode", "block": "wheels", "mode": "purchase"}])
    assert "wheel" in str(exc.value)


def test_set_binding_to_a_component_and_clear() -> None:
    tree = _tree(
        [
            *_WHEELS,
            {
                "op": "set_binding",
                "block": "wheel",
                "kind": "component",
                "design": "caster-wheel-100",
            },
        ]
    )
    assert tree.blocks["wheel"].bound_kind == "component"
    assert tree.blocks["wheel"].bound == "caster-wheel-100"
    apply_ops(tree, [{"op": "set_binding", "block": "wheel", "clear": True}])
    assert tree.blocks["wheel"].bound_kind is None


def test_set_binding_rejects_an_unknown_kind() -> None:
    with pytest.raises(OpError) as exc:
        _tree(
            [
                *_WHEELS,
                {
                    "op": "set_binding",
                    "block": "wheel",
                    "kind": "vibes",
                    "design": "x",
                },
            ]
        )
    assert "component" in str(exc.value)


# ── the demands this rung makes checkable ───────────────────────────────


def _bearing_joint(bom: list[Any]) -> SeTree:
    return _tree(
        [
            *_WHEELS,
            {"op": "add_block", "name": "axle", "parent": "cart"},
            {"op": "add_port", "block": "axle", "name": "shaft"},
            {"op": "add_port", "block": "wheel", "name": "bore"},
            {
                "op": "connect",
                "a": "axle.shaft",
                "b": "wheel.bore",
                "joint": {
                    "class": "revolute",
                    "axis": [1, 0, 0],
                    "mechanism": "bearing",
                },
            },
            *bom,
        ]
    )


def test_bearing_joint_without_a_bom_line_is_a_finding() -> None:
    report = se_drc.drc(_bearing_joint([]))
    findings = [f for f in report.findings if f.rule == "mechanism_bom"]
    assert len(findings) == 1
    assert "bearing" in findings[0].detail


def test_bearing_joint_satisfied_by_a_line_on_either_endpoint() -> None:
    tree = _bearing_joint(
        [
            {
                "op": "add_bom",
                "block": "wheel",
                "item_kind": "component",
                "item": "bearing-608",
                "qty": 2,
            }
        ]
    )
    report = se_drc.drc(tree)
    assert not [f for f in report.findings if f.rule == "mechanism_bom"]


def test_purchase_mode_naming_nothing_to_buy_is_a_finding() -> None:
    tree = _tree([*_WHEELS, {"op": "set_mode", "block": "wheel", "mode": "purchase"}])
    findings = [f for f in se_drc.drc(tree).findings if f.rule == "mode_without_item"]
    assert len(findings) == 1
    assert findings[0].subject == "wheel"


def test_purchase_mode_satisfied_by_a_line_on_the_block_itself() -> None:
    tree = _tree(
        [
            *_WHEELS,
            {"op": "set_mode", "block": "wheel", "mode": "purchase"},
            {
                "op": "add_bom",
                "block": "wheel",
                "item_kind": "component",
                "item": "caster-wheel-100",
            },
        ]
    )
    assert not [f for f in se_drc.drc(tree).findings if f.rule == "mode_without_item"]


def test_purchase_mode_not_satisfied_by_a_joints_bom_line() -> None:
    """A bearing bought for a joint the block sits on says nothing about
    what the block itself is."""
    tree = _bearing_joint(
        [
            {"op": "set_mode", "block": "wheel", "mode": "purchase"},
            {
                "op": "add_bom",
                "a": "axle.shaft",
                "b": "wheel.bore",
                "item_kind": "component",
                "item": "bearing-608",
                "qty": 2,
            },
        ]
    )
    findings = [f for f in se_drc.drc(tree).findings if f.rule == "mode_without_item"]
    assert [f.subject for f in findings] == ["wheel"]


def test_purchase_mode_satisfied_by_a_binding() -> None:
    tree = _tree(
        [
            *_WHEELS,
            {"op": "set_mode", "block": "wheel", "mode": "purchase"},
            {
                "op": "set_binding",
                "block": "wheel",
                "kind": "component",
                "design": "caster-wheel-100",
            },
        ]
    )
    assert not [f for f in se_drc.drc(tree).findings if f.rule == "mode_without_item"]


def test_unknown_stored_mode_surfaces_as_an_error() -> None:
    tree = _tree(_WHEELS)
    tree.blocks["wheel"].mode = "telepathy"  # hand-corrupted storage
    findings = [f for f in se_drc.drc(tree).findings if f.rule == "unknown_mode"]
    assert len(findings) == 1
    assert findings[0].severity == "error"


# ── round-trip + the rendered view ──────────────────────────────────────

_CART = json.dumps(
    {
        "description": "a four-wheeled cart",
        "ops": [
            *_WHEELS,
            {"op": "set_mode", "block": "wheel", "mode": "purchase"},
            {
                "op": "set_binding",
                "block": "wheel",
                "kind": "component",
                "design": "caster-wheel-100",
            },
            {
                "op": "add_bom",
                "block": "wheels",
                "item_kind": "component",
                "item": "bearing-608",
                "qty": 2,
                "reason": "one pair per wheel",
            },
        ],
    }
)


def test_bom_mode_and_binding_survive_a_round_trip(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="cart", text=_CART)
    ref = store.get_ref(kind="se", id="cart")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    assert tree.blocks["wheel"].mode == "purchase"
    assert tree.blocks["wheel"].bound_kind == "component"
    assert tree.blocks["wheel"].bound == "caster-wheel-100"
    (line,) = tree.bom
    assert (line.block, line.item, line.qty) == ("wheels", "bearing-608", 2.0)
    assert line.reason == "one pair per wheel"


def test_bom_view_prices_and_masses_through_the_component_kind(
    handler: SeHandler, hub: Hub
) -> None:
    components = ComponentHandler(hub=hub)
    components.put(
        id="bearing-608",
        title="608ZZ deep-groove ball bearing",
        category="bearing",
        uom="each",
    )
    components.put(id="bearing-608", spec="unit_cost", value=0.55, unit="USD")
    components.put(id="bearing-608", spec="mass", value=0.012, unit="kg")

    handler.put(id="cart", text=_CART)
    body = handler.get(id="cart", view="bom").body

    assert "608ZZ deep-groove ball bearing" in body
    assert "wheels 2×4" in body  # 2 per member, 4 members
    # 8 × 0.55 and 8 × 0.012 — the totals come from the component kind's
    # own current-value authority, never a copy of it.
    assert "unit_cost total: 4.4" in body
    assert "mass total: 0.096" in body


def test_bom_view_reports_an_item_that_is_not_in_the_store(
    handler: SeHandler,
) -> None:
    """Naming what you intend to buy before it's in the store is legal
    (suggestive by contract) — the view says so and leaves it out of the
    totals, rather than rejecting the write or inventing a price."""
    handler.put(
        id="unsourced",
        text=json.dumps(
            {
                "ops": [
                    *_WHEELS,
                    {
                        "op": "add_bom",
                        "block": "wheels",
                        "item_kind": "component",
                        "item": "no-such-bearing-in-the-store",
                    },
                ]
            }
        ),
    )
    body = handler.get(id="unsourced", view="bom").body
    assert "not in the store" in body
    assert "component:no-such-bearing-in-the-store" in body
    assert "unit_cost total: 0" in body


def test_bom_view_says_mixed_uom_instead_of_a_bogus_total(
    handler: SeHandler,
) -> None:
    handler.put(
        id="mixed",
        text=json.dumps(
            {
                "ops": [
                    *_WHEELS,
                    {
                        "op": "add_bom",
                        "block": "wheel",
                        "item_kind": "component",
                        "item": "alu-tube-20",
                        "qty": 0.4,
                        "uom": "m",
                    },
                    {
                        "op": "add_bom",
                        "block": "cart",
                        "item_kind": "component",
                        "item": "alu-tube-20",
                        "qty": 2,
                        "uom": "each",
                    },
                ]
            }
        ),
    )
    body = handler.get(id="mixed", view="bom").body
    assert "mixed uom" in body
    assert "each" in body and " m" in body


def test_bom_view_of_a_design_that_buys_nothing(handler: SeHandler) -> None:
    handler.put(id="bare", text=json.dumps({"ops": _WHEELS}))
    body = handler.get(id="bare", view="bom").body
    assert "nothing bought yet" in body


def test_block_view_shows_mode_binding_and_realization_count(
    handler: SeHandler,
) -> None:
    handler.put(id="cart", text=_CART)
    body = handler.get(id="cart", view="block", args={"name": "wheel"}).body
    assert "mode: purchase" in body
    assert "realization: component:caster-wheel-100" in body
    assert "realized: ×5" in body


def test_unknown_view_lists_bom(handler: SeHandler) -> None:
    handler.put(id="cart", text=_CART)
    with pytest.raises(BadInput) as exc:
        handler.get(id="cart", view="nope")
    assert "bom" in str(exc.value.next)
    assert "bom" in SeHandler.spec.views
