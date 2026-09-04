"""Bought items on the tree — the BOM line and its multiplicity rollup.

se-kind.md's Decisions are explicit: **a thing bought is never a block.**
A block is a thing *made* (from stock, from a printer, from other
blocks); a bearing, a screw, a length of pipe is a ``component``/``part``
link with a quantity, hung off the block or the connect that needs it
(``se_bom``, migration ``0003``).

This module is pure over an :class:`~precis_se.ops.SeTree` — no store
access, no catalog lookup. It owns two things:

- the stored shape of a line (:class:`BomLine`, vetted by
  :func:`vet_bom_fields` for both writers), and
- the **multiplicity rollup** (:func:`rollup`), which is the whole reason
  a BOM over an se design isn't just a list: a design's arrays and
  instances mean one authored line is many bought things, and getting
  that arithmetic wrong is how you order 4 bearings for a 20-bearing
  machine.

**Occurrence arithmetic**, following the tree's own read-time expansion
(``handler._render_tree``'s walk, verbatim semantics — a BOM that
disagreed with the tree would be a second truth):

- Every block is placed once by its own row; an **array node stands for
  its ``count`` members**, so its occurrence count is that count, and
  everything below it multiplies through.
- A **template is itself a placed block** (``instance_block`` requires an
  ordinary block to instance, and the tree renders that block where it
  sits) — so a template's design is realized once for the template *plus*
  once per referencing instance, or ``count`` times per referencing
  array. A line on the template is therefore counted for all of them; a
  line on the array node is counted for its members only.
- A line on a **connect** takes the larger of its two endpoints'
  realization counts — the arrayed side drives it (four wheels on one
  axle need four sets of bearings, not one). When the two differ the
  rollup says so rather than hiding the choice.

A line naming a block/connect that isn't in the tree resolves to no
count at all (``None``): it is rendered, excluded from the totals, and
reported by DRC — never silently counted as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: What a line may point at. ``component`` is the engineering store (a
#: slug), ``part`` the LCSC/JLCPCB catalog (a C-number).
ITEM_KINDS: tuple[str, ...] = ("component", "part")


class BomError(ValueError):
    """A rejected BOM line (bad item kind, empty item, non-positive qty)."""


@dataclass
class BomLine:
    """One bought item hung off a block or a connect. Exactly one target
    form is set — ``block``, or the four connect endpoint names — which
    :func:`vet_bom_fields` enforces for both writers. ``qty`` is **per
    occurrence** of that target (see the module docstring); ``uom``
    overrides the item's own unit of measure for this line only."""

    item_kind: str
    item: str
    qty: float = 1.0
    block: str | None = None
    a_block: str | None = None
    a_port: str | None = None
    b_block: str | None = None
    b_port: str | None = None
    uom: str | None = None
    reason: str | None = None

    @property
    def is_connect(self) -> bool:
        return self.block is None

    @property
    def target(self) -> str:
        """The human/agent-facing target label — ``'hub'`` or
        ``'hub.bore—wheel.hub'``, the same subject shape DRC findings
        use."""
        if self.block is not None:
            return self.block
        return f"{self.a_block}.{self.a_port}—{self.b_block}.{self.b_port}"

    @property
    def endpoints(self) -> tuple[str, str] | None:
        """The two endpoint *block* names of a connect line, or ``None``
        for a block line."""
        if self.block is not None:
            return None
        assert self.a_block is not None and self.b_block is not None
        return (self.a_block, self.b_block)


@dataclass
class BomTotal:
    """One rolled-up item: every line naming it, and the total quantity.
    ``total`` is ``None`` only when *no* contributing line resolved to a
    count (every target missing) — a partial resolution still totals what
    it can and says how much it left out."""

    item_kind: str
    item: str
    total: float | None
    uom: str | None
    #: ``(target label, per-occurrence qty, occurrences or None)`` per line.
    contributions: list[tuple[str, float, int | None]] = field(default_factory=list)
    #: every distinct uom override seen across the contributing lines —
    #: more than one means the quantities being summed aren't in the same
    #: unit, which the renderer must say rather than quietly pick one.
    uoms: set[str] = field(default_factory=set)

    @property
    def unresolved(self) -> int:
        return sum(1 for _, _, occ in self.contributions if occ is None)

    @property
    def mixed_uom(self) -> bool:
        return len(self.uoms) > 1


def vet_bom_fields(
    *,
    item_kind: Any,
    item: Any,
    qty: Any,
    uom: Any = None,
    reason: Any = None,
    opname: str,
) -> tuple[str, str, float, str | None, str | None]:
    """Vet the item half of a line, raising :class:`BomError` with the
    legal vocabulary on any miss. The target half is resolved by the
    caller (it needs the tree)."""
    kind = str(item_kind or "").strip()
    if kind not in ITEM_KINDS:
        known = " | ".join(ITEM_KINDS)
        raise BomError(f"{opname}: 'item_kind' must be one of {known}; got {kind!r}")
    slug = str(item or "").strip()
    if not slug:
        raise BomError(
            f"{opname} needs 'item' — the {kind} "
            f"{'slug' if kind == 'component' else 'C-number'} to buy"
        )
    try:
        quantity = float(1.0 if qty is None else qty)
    except (TypeError, ValueError) as exc:
        raise BomError(f"{opname}: 'qty' must be a number, got {qty!r}") from exc
    if not quantity > 0.0:
        raise BomError(f"{opname}: 'qty' must be > 0, got {quantity:g}")
    unit = str(uom).strip() if uom is not None and str(uom).strip() else None
    why = str(reason).strip() if reason is not None and str(reason).strip() else None
    return kind, slug, quantity, unit, why


def node_occurrences(tree: Any) -> dict[str, int]:
    """``{block name: how many times that *node* is placed}``, by the same
    read-time expansion the tree view walks: an array node counts as its
    member count, and everything below multiplies through.

    Defensive against a stored instance cycle (hand-corrupted data) the
    same way the renderer is — the path set stops the walk instead of
    recursing forever."""
    counts: dict[str, int] = {name: 0 for name in tree.blocks}
    children: dict[str | None, list[str]] = {}
    for name, node in tree.blocks.items():
        children.setdefault(node.parent, []).append(name)

    def _walk(name: str, multiplier: int, path: tuple[str, ...]) -> None:
        node = tree.blocks[name]
        own = multiplier
        if node.array:
            try:
                own = multiplier * int(node.array.get("count", 1))
            except (TypeError, ValueError):  # pragma: no cover — ops vets count
                own = multiplier
        counts[name] = counts.get(name, 0) + own
        source = node.template or name
        if source in path:
            return
        for child in sorted(children.get(source, [])):
            _walk(child, own, (*path, source))

    for root in sorted(children.get(None, [])):
        _walk(root, 1, ())
    return counts


def design_occurrences(tree: Any) -> dict[str, int]:
    """``{block name: how many times that block's *design* is realized}``
    — its own placements plus every instance/array node standing on it.

    This is the number a BOM line wants: "each realization of the wheel
    needs 2 bearings" must count the instanced wheels too, not only the
    template's own placement."""
    node_occ = node_occurrences(tree)
    out = dict(node_occ)
    for name, node in tree.blocks.items():
        if node.template is not None and node.template in out:
            out[node.template] += node_occ.get(name, 0)
    return out


def line_occurrences(
    tree: Any, line: BomLine, *, occ: dict[str, int] | None = None
) -> tuple[int | None, str | None]:
    """``(occurrences, note)`` for one line: how many times its target is
    realized, and a note when that number needed a judgment call (the
    connect endpoints disagreeing) or couldn't be made at all. Pass a
    precomputed ``occ`` when resolving many lines over one tree."""
    if occ is None:
        occ = design_occurrences(tree)
    if line.block is not None:
        if line.block not in tree.blocks:
            return None, f"no block named {line.block!r}"
        return occ[line.block], None
    endpoints = line.endpoints
    assert endpoints is not None
    a, b = endpoints
    missing = [n for n in (a, b) if n not in tree.blocks]
    if missing:
        return None, f"no block named {', '.join(repr(m) for m in missing)}"
    a_occ, b_occ = occ[a], occ[b]
    if a_occ == b_occ:
        return a_occ, None
    winner, loser = (a, b) if a_occ > b_occ else (b, a)
    return max(a_occ, b_occ), (
        f"endpoints differ ({a}×{a_occ}, {b}×{b_occ}) — took {winner}'s "
        f"count, not {loser}'s"
    )


def rollup(tree: Any) -> list[BomTotal]:
    """Aggregate every line to one row per bought item, quantities
    multiplied through the tree's multiplicities. Sorted by item kind then
    item, so the render is stable."""
    by_item: dict[tuple[str, str], BomTotal] = {}
    occ = design_occurrences(tree)
    for line in tree.bom:
        key = (line.item_kind, line.item)
        total = by_item.get(key)
        if total is None:
            total = BomTotal(
                item_kind=line.item_kind, item=line.item, total=None, uom=line.uom
            )
            by_item[key] = total
        occurrences, _note = line_occurrences(tree, line, occ=occ)
        total.contributions.append((line.target, line.qty, occurrences))
        if occurrences is not None:
            total.total = (total.total or 0.0) + line.qty * occurrences
        if line.uom:
            total.uoms.add(line.uom)
        if total.uom is None:
            total.uom = line.uom
    return [by_item[k] for k in sorted(by_item)]
