"""``realized-by`` → ``component``: the order rollup — blocktree slice 6
(docs/backlog/blocktree-library-build-plan.md §Slice 6). "What do I
order": walk the instanced tree to purchasable leaves, the way
``view='fab'`` already does (:meth:`precis_se.handler.SeHandler.
_render_fab`), classify each live local block by its own
``bound_kind``/``bound``, and report one line per distinct purchasable
item — merged with the design's explicit BOM lines
(:mod:`precis_se.bom`) naming the same item, and with a "bought whole"
assembly's subtree skipped entirely — plus a to-make table for
everything left over.

Store-aware, like :mod:`precis_se.precedent`, its slice-5 sibling — but
takes the PLAIN ``store`` and calls
``store.component_current_spec_value`` itself rather than the handler's
bound ``_spec_number``: the handler renders THIS module's report, so
importing the handler back would cycle. The ``realized-by`` link is
already derived on every save (``persist.sync_realized_by``); nothing
here writes anything — the walk reads the binding that link mirrors
(``block.bound_kind``/``block.bound``), never the link table.

**The walk.** ``bound_kind``/``bound``/``mode`` live only on ordinary/
template blocks (an instance/array node's own uid never carries them —
``_template_owned`` refuses ``set_binding``/``set_mode`` there), and
:func:`precis_se.bom.design_occurrences` already folds every instance's
and array's count into its template's total. So: iterate the live local
blocks, skip any node with ``node.template is not None`` (exactly as
``_render_fab`` does), and read binding/mode off what's left, with
``qty = design_occurrences[name]``.

**Cross-design.** A foreign-templated instance (``'<slug>#<block>'``) has
no local template block — :func:`precis_se.bom.design_occurrences` never
touches ``tree.foreign`` (its own module docstring), so this module adds
that leg itself: for every local node whose ``template`` is
cross-design-qualified, sum that node's own occurrence count
(:func:`~precis_se.bom.node_occurrences`, keyed by the LOCAL node — its
own array multiplicity along its own parents) onto the qualified template
name, then resolve the binding through
:func:`precis.blocktree.ops.resolve_template`/``tree.foreign``. The
occurrence count stays the borrowing design's own; only the binding comes
from the foreign design.

**Bought assemblies.** A template is a **leaf** when no live local block
has it as ``parent``. A non-leaf template that is itself bound to a
component/part is purchasable, and its subtree is NOT walked — its
descendants are covered by the purchase, not separately ordered or made.

**To-make is leaf-only.** An UNBOUND non-leaf template — an assembly not
bought whole — gets no row of its own; it's a container, not a thing to
buy or make, and its children (walked normally, since they were never
added to the hidden set above) carry the to-make weight instead. Only a
BOUND non-leaf hides its children (the opposite direction); an unbound
one never hides anything, it simply isn't listed itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.blocktree.types import parse_template_ref
from precis_se import bom as se_bom
from precis_se.ops import SeTree, resolve_template

#: Realization bindings that name something bought rather than made
#: (mirrors ``se_blocks_bound_kind_check`` — the third legal value,
#: ``'structure'``, is atomic-mode realization, never purchasable).
_PURCHASABLE_KINDS: tuple[str, ...] = ("component", "part")


@dataclass
class PurchasableContribution:
    """One provenance feeding a merged purchasable line — either a bound
    template (``source='binding'``, ``label`` its name, qty already
    multiplied through :func:`~precis_se.bom.design_occurrences`/the
    foreign leg above) or the design's own BOM lines naming the same item
    (``source='bom line'``, qty already :func:`~precis_se.bom.rollup`'s
    own occurrence-multiplied total — ``None`` when every BOM line
    targeting it was itself unresolved)."""

    label: str
    qty: float | None
    source: str  # 'binding' | 'bom line'
    #: Set only on a bound template whose subtree was skipped (see the
    #: module docstring's "bought assemblies") — how many further blocks
    #: this one purchase covers.
    covers: int | None = None


@dataclass
class PurchasableLine:
    """One order line: everything the design needs of one ``component``
    slug or ``part`` C-number, whatever bound it — a leaf template, a
    bought-whole assembly, or an explicit BOM line — merged so the same
    item is never ordered twice on two rows. ``total`` is ``None`` only
    when every contribution's own quantity was itself unresolved."""

    item_kind: str  # 'component' | 'part'
    item: str
    qty: float | None = None
    contributions: list[PurchasableContribution] = field(default_factory=list)
    #: A ``component``'s own title (``store.get_ref``) — parts carry no
    #: such record, so this stays ``None`` for a ``part`` line.
    label: str | None = None
    unit_cost: float | None = None
    mass: float | None = None
    category: str | None = None
    mpn: str | None = None
    #: ``False`` only for a ``component`` slug absent from the store — a
    #: ``part`` line is never priced/massed (no store record exists for
    #: one) but is not "missing" the way an unregistered component is.
    in_store: bool = True


@dataclass
class ToMakeRow:
    """A live template with no purchasable binding — ``cad``,
    ``structure``, or simply undecided."""

    block: str
    mode: str | None
    qty: float


@dataclass
class OrderReport:
    """``rollup``'s whole answer: the purchasable table, the to-make
    table, and whether the design has any blocks at all (the "unfilled"
    empty case is distinct from "nothing purchasable yet")."""

    has_blocks: bool
    purchasable: list[PurchasableLine] = field(default_factory=list)
    to_make: list[ToMakeRow] = field(default_factory=list)


def _spec_number(store: Any, ref_id: int, spec_id: str) -> float | None:
    """One component's current numeric value for ``spec_id`` — the same
    single "current value" authority :meth:`precis_se.handler.SeHandler.
    _spec_number` reads, called directly here since a plain store is all
    this module gets (module docstring: no handler import, no cycle)."""
    row = store.component_current_spec_value(ref_id, spec_id)
    if row is None:
        return None
    value = row.get("value_num")
    return None if value is None else float(value)


def _descendants(children: dict[str | None, list[str]], root: str) -> set[str]:
    """Every block name reachable from ``root`` by the physical
    ``parent`` chain — the subtree a bought-whole assembly covers,
    regardless of whether each member is itself an ordinary/template
    block or an instance/array node standing on one."""
    out: set[str] = set()
    stack = list(children.get(root, []))
    while stack:
        name = stack.pop()
        if name in out:
            continue
        out.add(name)
        stack.extend(children.get(name, []))
    return out


def rollup(store: Any, tree: SeTree, ref_id: int) -> OrderReport:
    """The order report over ``tree`` — see the module docstring for the
    walk. ``ref_id`` is accepted for symmetry with :mod:`precis_se.
    precedent`'s ``findings`` signature (a future round may need it to
    scope a store read to this design); the rollup itself needs only the
    store and the tree."""
    if not tree.blocks:
        return OrderReport(has_blocks=False)

    children: dict[str | None, list[str]] = {}
    for name, node in tree.blocks.items():
        children.setdefault(node.parent, []).append(name)

    occ = se_bom.design_occurrences(tree)
    node_occ = se_bom.node_occurrences(tree)

    # The foreign leg `design_occurrences` never computes (its own module
    # docstring: it never touches `tree.foreign`) — one local node's own
    # occurrence count per foreign-qualified template it instances.
    foreign_occ: dict[str, int] = {}
    for name, node in tree.blocks.items():
        if node.template is None:
            continue
        design_slug, _ = parse_template_ref(node.template)
        if design_slug is None:
            continue  # bare local template — already folded into `occ`
        foreign_occ[node.template] = foreign_occ.get(node.template, 0) + node_occ.get(
            name, 0
        )

    # Bought-whole assemblies: a non-leaf LOCAL template that is itself
    # bound — its subtree is never walked (module docstring). Computed
    # before classification so the main walk can just skip hidden names.
    hidden: set[str] = set()
    covers: dict[str, int] = {}
    for name, node in tree.blocks.items():
        if node.template is not None:
            continue
        if node.bound_kind not in _PURCHASABLE_KINDS or not node.bound:
            continue
        if name not in children:  # a leaf — the ordinary purchasable case
            continue
        below = _descendants(children, name)
        covers[name] = len(below)
        hidden |= below

    by_item: dict[tuple[str, str], PurchasableLine] = {}
    to_make: list[ToMakeRow] = []

    def _line(item_kind: str, item: str) -> PurchasableLine:
        key = (item_kind, item)
        line = by_item.get(key)
        if line is None:
            line = PurchasableLine(item_kind=item_kind, item=item)
            by_item[key] = line
        return line

    def _add_qty(line: PurchasableLine, qty: float | None) -> None:
        if qty is None:
            return
        line.qty = qty if line.qty is None else line.qty + qty

    def _classify(name: str, node: Any, qty: float, *, covers_n: int | None) -> None:
        if node.bound_kind in _PURCHASABLE_KINDS and node.bound:
            line = _line(node.bound_kind, node.bound)
            line.contributions.append(
                PurchasableContribution(
                    label=name, qty=qty, source="binding", covers=covers_n
                )
            )
            _add_qty(line, qty)
        elif name not in children:  # a leaf — the ordinary to-make case
            to_make.append(ToMakeRow(block=name, mode=node.mode, qty=qty))
        # else: an unbound non-leaf template is an assembly of the things
        # below it, not a thing in its own right to make/buy — no row of
        # its own; its children, walked separately below, carry the
        # to-make weight instead (a BOUND non-leaf is the "bought whole"
        # exception above, hiding its children rather than the reverse).

    for name in sorted(tree.blocks):
        if name in hidden:
            continue
        node = tree.blocks[name]
        if node.template is not None:
            continue  # instance/array node — no binding/mode of its own
        _classify(name, node, float(occ.get(name, 0)), covers_n=covers.get(name))

    for template in sorted(foreign_occ):
        foreign_node = resolve_template(tree, template)
        if foreign_node is None:
            continue  # dangling foreign template — nothing to classify
        _classify(template, foreign_node, float(foreign_occ[template]), covers_n=None)

    # Explicit BOM lines merge into the same purchasable table (already
    # occurrence-multiplied and merged-by-item — `se_bom.rollup`'s own
    # contract).
    for total in se_bom.rollup(tree):
        if total.item_kind not in _PURCHASABLE_KINDS:
            continue
        line = _line(total.item_kind, total.item)
        line.contributions.append(
            PurchasableContribution(
                label="bom line", qty=total.total, source="bom line"
            )
        )
        _add_qty(line, total.total)

    for line in by_item.values():
        if line.item_kind != "component":
            continue  # a `part` C-number has no store record to resolve
        ref = store.get_ref(kind="component", id=line.item)
        if ref is None:
            line.in_store = False
            continue
        meta = ref.meta or {}
        line.label = str(ref.title or ref.slug or ref.id)
        line.category = meta.get("category")
        line.mpn = meta.get("mpn")
        line.unit_cost = _spec_number(store, ref.id, "unit_cost")
        line.mass = _spec_number(store, ref.id, "mass")

    purchasable = [by_item[k] for k in sorted(by_item)]
    to_make.sort(key=lambda r: r.block)
    return OrderReport(has_blocks=True, purchasable=purchasable, to_make=to_make)
