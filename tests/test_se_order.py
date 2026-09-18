"""``realized-by`` → ``component`` — blocktree slice 6
(docs/backlog/blocktree-library-build-plan.md §Slice 6,
:mod:`precis_se.order`, ``view='order'``).

Covers the walk's load-bearing cases: a leaf template bound to a
component and instanced through an array counts once with the
array-multiplied qty; two blocks bound to the same slug merge; a bought
assembly hides its children and says ``covers N``; a ``part`` line is
unpriced; the partial-total honesty rule; a foreign template's bound
component is counted for the *borrowing* design (the adversarial
cross-design case); and an explicit BOM line plus a bound leaf naming the
same slug do not double count.

Same fixture shape as ``test_se_bom.py``: the shared test DB template
carries only core migrations, so this module seeds the plugin's own
directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import precis_se
from precis.dispatch import Hub
from precis.handlers.component import ComponentHandler
from precis.store import Store
from precis_se import order as se_order
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.ops import SeTree, apply_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


def _apply_se_migrations(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    _apply_se_migrations(store)
    return SeHandler(hub=hub)


def _tree(ops: list[Any]) -> SeTree:
    return apply_ops(SeTree(), ops)


def _components(hub: Hub) -> ComponentHandler:
    return ComponentHandler(hub=hub)


def _load(store: Store, slug: str) -> tuple[Any, int]:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(store, ref.id), ref.id


#: Mirrors ``test_se_bom.py``'s ``_WHEELS`` fixture: a wheel template
#: placed once, plus a 4-up array of it — 5 realizations of "wheel".
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


# ── a leaf template bound + instanced through an array counts ONCE ──────


def test_bound_leaf_through_an_array_counts_once_with_multiplied_qty(
    handler: SeHandler, hub: Hub, store: Store
) -> None:
    _components(hub).put(id="caster-wheel-100", title="Caster wheel", category="wheel")
    handler.put(
        id="array-order1",
        text=json.dumps(
            {
                "ops": [
                    *_WHEELS,
                    {
                        "op": "set_binding",
                        "block": "wheel",
                        "kind": "component",
                        "design": "caster-wheel-100",
                    },
                ]
            }
        ),
    )
    tree, ref_id = _load(store, "array-order1")
    report = se_order.rollup(store, tree, ref_id)
    (line,) = report.purchasable
    assert line.item_kind == "component"
    assert line.item == "caster-wheel-100"
    # the template's own placement (1) + the array's 4 members
    assert line.qty == pytest.approx(5.0)
    # the array node itself (`node.template is not None`) never becomes a
    # second contribution — only the template block does.
    assert [c.label for c in line.contributions] == ["wheel"]


# ── two blocks bound to the same slug merge into one line ───────────────


def test_two_blocks_bound_to_the_same_slug_merge_with_both_names(
    handler: SeHandler, hub: Hub, store: Store
) -> None:
    _components(hub).put(id="m6-bolt", title="M6 bolt", category="fastener")
    _components(hub).put(id="m6-bolt", spec="unit_cost", value=0.1, unit="USD")
    handler.put(
        id="merge-order1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "a"},
                    {"op": "add_block", "name": "b"},
                    {
                        "op": "set_binding",
                        "block": "a",
                        "kind": "component",
                        "design": "m6-bolt",
                    },
                    {
                        "op": "set_binding",
                        "block": "b",
                        "kind": "component",
                        "design": "m6-bolt",
                    },
                ]
            }
        ),
    )
    tree, ref_id = _load(store, "merge-order1")
    report = se_order.rollup(store, tree, ref_id)
    (line,) = report.purchasable
    assert line.qty == pytest.approx(2.0)
    used_by = sorted(c.label for c in line.contributions)
    assert used_by == ["a", "b"]

    # ONE merged line, but the honesty header counts the two TEMPLATES
    # that fed it — `purchasable`/`to make` are template counts, while
    # `priced`/`massed` stay LINE counts (reviewer finding 1).
    body = handler.get(id="merge-order1", view="order").body
    assert "purchasable: 2 of 2 leaf template(s) · to make: 0" in body
    assert "priced: 1 of 1 line(s)" in body


# ── a bought-whole assembly hides its children ───────────────────────────


def test_bought_assembly_hides_children_and_says_covers_n(
    handler: SeHandler, hub: Hub, store: Store
) -> None:
    _components(hub).put(
        id="gearbox-assy", title="Gearbox assembly", category="assembly"
    )
    handler.put(
        id="assy-order1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "gearbox"},
                    {"op": "add_block", "name": "shaft", "parent": "gearbox"},
                    {"op": "add_block", "name": "cog", "parent": "shaft"},
                    {
                        "op": "set_binding",
                        "block": "gearbox",
                        "kind": "component",
                        "design": "gearbox-assy",
                    },
                ]
            }
        ),
    )
    tree, ref_id = _load(store, "assy-order1")
    report = se_order.rollup(store, tree, ref_id)
    (line,) = report.purchasable
    assert line.item == "gearbox-assy"
    (contribution,) = line.contributions
    assert contribution.covers == 2  # shaft + cog, both hidden
    assert report.to_make == []  # shaft/cog never separately listed
    body = handler.get(id="assy-order1", view="order").body
    assert "covers 2 block(s)" in body
    assert "shaft" not in body
    assert "cog" not in body


# ── a `part` line is unpriced ────────────────────────────────────────────


def test_part_line_is_unpriced_and_the_honesty_line_says_so(
    handler: SeHandler,
) -> None:
    handler.put(
        id="part-order1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "pcb"},
                    {
                        "op": "set_binding",
                        "block": "pcb",
                        "kind": "part",
                        "design": "C123456",
                    },
                ]
            }
        ),
    )
    body = handler.get(id="part-order1", view="order").body
    assert "part:C123456" in body
    assert "priced: 0 of 1 line(s)" in body
    assert "massed: 0 of 1 line(s)" in body
    assert "partial, 0 of 1" in body


# ── the partial-total rule ───────────────────────────────────────────────


def test_partial_total_rule_when_not_every_line_is_priced(
    handler: SeHandler, hub: Hub, store: Store
) -> None:
    _components(hub).put(id="priced-part", title="Priced widget", category="widget")
    _components(hub).put(id="priced-part", spec="unit_cost", value=2.0, unit="USD")
    handler.put(
        id="partial-order1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "a"},
                    {"op": "add_block", "name": "b"},
                    {
                        "op": "set_binding",
                        "block": "a",
                        "kind": "component",
                        "design": "priced-part",
                    },
                    {
                        "op": "set_binding",
                        "block": "b",
                        "kind": "part",
                        "design": "C999",
                    },
                ]
            }
        ),
    )
    body = handler.get(id="partial-order1", view="order").body
    assert "priced: 1 of 2 line(s)" in body
    assert "total: ≥ 2 (partial, 1 of 2)" in body

    # A fully priced design prints an unconditional total instead.
    tree, ref_id = _load(store, "partial-order1")
    handler2 = handler
    handler2.edit(id="partial-order1", ops=[{"op": "remove_block", "block": "b"}])
    full_body = handler2.get(id="partial-order1", view="order").body
    assert "priced: 1 of 1 line(s)" in full_body
    assert "total: 2" in full_body
    assert "partial" not in full_body
    del tree, ref_id  # unused beyond illustrating the store/tree pattern


def test_priced_component_named_only_by_a_dangling_bom_line_stays_partial(
    handler: SeHandler, hub: Hub, store: Store
) -> None:
    """A priced component whose only order-line contribution is a BOM
    line naming a block no longer in the tree: the PRICE resolved, but
    the QTY did not (``se_bom.rollup``'s own unresolved-target rule), so
    the honesty header must still read as unpriced/partial — never a
    bare ``total:`` implying a real number (reviewer finding 2)."""
    _components(hub).put(
        id="stale-priced", title="Stale priced widget", category="widget"
    )
    _components(hub).put(id="stale-priced", spec="unit_cost", value=5.0, unit="USD")
    tree = _tree([{"op": "add_block", "name": "solo"}])
    # Hand-corrupted row (ops' vacancy rules never leave one of these —
    # test_se_bom.py's own defense-in-depth pattern): the target block
    # isn't in the tree, so `se_bom.rollup` can't resolve an occurrence
    # count for it.
    from precis_se.bom import BomLine

    tree.bom.append(
        BomLine(item_kind="component", item="stale-priced", qty=2, block="ghost")
    )
    body = handler._render_order(tree, ref_id=0)
    (line,) = [line for line in body.splitlines() if line.startswith("total:")]
    assert line == "total: ≥ 0 (partial, 0 of 1)"
    assert "priced: 0 of 1 line(s)" in body


# ── unbound non-leaf assemblies get no row; their leaf children do ──────


def test_unbound_non_leaf_assembly_is_absent_its_leaf_children_are_not(
    handler: SeHandler, store: Store
) -> None:
    handler.put(
        id="unbound-assembly-order1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "frame"},
                    {"op": "add_block", "name": "left", "parent": "frame"},
                    {"op": "add_block", "name": "right", "parent": "frame"},
                ]
            }
        ),
    )
    tree, ref_id = _load(store, "unbound-assembly-order1")
    report = se_order.rollup(store, tree, ref_id)
    assert {r.block for r in report.to_make} == {"left", "right"}
    body = handler.get(id="unbound-assembly-order1", view="order").body
    assert "to make: 2" in body
    assert "frame" not in body


# ── cross-design: a foreign template's bound component counts locally ───


def test_foreign_templates_bound_component_counted_for_borrowing_design(
    handler: SeHandler, hub: Hub, store: Store
) -> None:
    _components(hub).put(
        id="library-bearing", title="Library bearing", category="bearing"
    )
    handler.put(
        id="order-library1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "part",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "set_binding",
                        "block": "part",
                        "kind": "component",
                        "design": "library-bearing",
                    },
                ]
            }
        ),
    )
    handler.put(
        id="order-consumer1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "host"},
                    {
                        "op": "instance_block",
                        "name": "borrowed",
                        "parent": "host",
                        "template": "order-library1#part",
                    },
                    {
                        "op": "array_block",
                        "name": "borrowed_array",
                        "template": "order-library1#part",
                        "parent": "host",
                        "linear": {"count": 3, "pitch": 0.02, "axis": [1, 0, 0]},
                    },
                ]
            }
        ),
    )
    tree, ref_id = _load(store, "order-consumer1")
    report = se_order.rollup(store, tree, ref_id)
    (line,) = report.purchasable
    assert line.item == "library-bearing"
    # the direct instance (1) + the 3-member array — the BORROWING
    # design's own local occurrence count, never the library's.
    assert line.qty == pytest.approx(4.0)
    assert line.contributions[0].label == "order-library1#part"

    # The library design's own rollup does not double count the
    # consumer's instances — its own placement is 1.
    library_tree, library_ref_id = _load(store, "order-library1")
    library_report = se_order.rollup(store, library_tree, library_ref_id)
    (library_line,) = library_report.purchasable
    assert library_line.qty == pytest.approx(1.0)


# ── an explicit BOM line + a bound leaf for the same slug merge ─────────


def test_bom_line_and_bound_leaf_same_slug_do_not_double_count(
    handler: SeHandler, hub: Hub, store: Store
) -> None:
    _components(hub).put(id="shared-slug", title="Shared item", category="misc")
    handler.put(
        id="bom-merge-order1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "bracket"},
                    {
                        "op": "set_binding",
                        "block": "bracket",
                        "kind": "component",
                        "design": "shared-slug",
                    },
                    {
                        "op": "add_block",
                        "name": "elsewhere",
                    },
                    {
                        "op": "add_bom",
                        "block": "elsewhere",
                        "item_kind": "component",
                        "item": "shared-slug",
                        "qty": 2,
                    },
                ]
            }
        ),
    )
    tree, ref_id = _load(store, "bom-merge-order1")
    report = se_order.rollup(store, tree, ref_id)
    assert len(report.purchasable) == 1  # ONE line, not two
    (line,) = report.purchasable
    # bracket's own occurrence (1) plus the bom line's qty (2)
    assert line.qty == pytest.approx(3.0)
    sources = sorted(c.source for c in line.contributions)
    assert sources == ["binding", "bom line"]
    # `elsewhere` is not itself unbound-to-make AND separately purchasable
    # — it's plain to-make (the bom line hangs off it, doesn't bind it).
    assert {r.block for r in report.to_make} == {"elsewhere"}


# ── empty cases ───────────────────────────────────────────────────────


def test_no_blocks_yet_reads_as_unfilled(handler: SeHandler) -> None:
    handler.put(id="empty-order1", text="{}")
    body = handler.get(id="empty-order1", view="order").body
    assert "no blocks yet — unfilled" in body


def test_blocks_but_nothing_purchasable_and_no_bom_lines(handler: SeHandler) -> None:
    handler.put(id="bare-order1", text=json.dumps({"ops": _WHEELS}))
    body = handler.get(id="bare-order1", view="order").body
    assert "nothing to order yet" in body
    assert "## to make" in body


# ── unknown-view help lists `order` ──────────────────────────────────────


def test_unknown_view_lists_order(handler: SeHandler) -> None:
    handler.put(id="unknown-view-order1", text="{}")
    from precis.errors import BadInput

    with pytest.raises(BadInput) as exc:
        handler.get(id="unknown-view-order1", view="nope")
    assert "order" in str(exc.value.next)
