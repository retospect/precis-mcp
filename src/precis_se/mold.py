"""``mold.undercut`` — the se-side mold-release finding (spec
``docs/backlog/se-cylinder-wrap-mold.md``, slice 2).

Every block bound to a cad design (:class:`~precis_se.ops.SeTree`
``bound_kind='cad'``) is hydrated through the same store-level seam
:mod:`precis_se.printsolid` uses, and its relief leaves are checked
against their own slide pull — a leaf's recorded ``k``/``alpha``/``phi``
when it has intent (a pull-aware generator stamps it), the argmin-over-k
layout when it does not (a hand-made map gets no benefit of the doubt).

An undercut leaf lands an ``error`` finding naming the layout, the
undercut fraction and area, and the smallest ``k`` that would clear it —
or that none would, pointing at ``relief_fit='clip'`` on the cad
``view='mold'`` report (the image lives there; DRC refuses the map until
clipped). A block whose bound design cannot be resolved is skipped —
the dangling-binding rule owns that complaint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.cad.dsl import DslError
from precis.cad.mold import (
    MoldLayout,
    MoldUndercut,
    SlideLayout,
    check_leaf,
    cut_mold,
    mold_undercut_reasons,
)
from precis.cad.primitives import CylRelief
from precis.cad.scene import SceneError, SceneSpec, build_design, expand_instances
from precis.cad_resolve import design_resolver
from precis_se.modes import ModeError, parse_mode
from precis_se.ops import OpError, SeBlock, SeTree, apply_ops
from precis_se.realize import _unique_cad_slug
from precis_se.validate import ValidationIssue

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store

__all__ = ["RULE", "PendingMold", "findings", "finish_mold", "prepare_mold"]

RULE = "mold.undercut"

#: The mode family the molded part itself takes — slice 3's new family in
#: :mod:`precis_se.modes`. The mold *pieces* are prototyped on the existing
#: ``sla`` family (spec Q4: SLA-printed halves).
MOLD_PART_MODE = "molding"
PIECE_MODE = "sla"


def findings(store: Any, tree: SeTree, ref_id: int) -> list[ValidationIssue]:
    """One finding per relief leaf that leans out of its slide's release
    cone — ``error``: a mold with this map does not open."""
    out: list[ValidationIssue] = []
    for name, node in sorted(tree.blocks.items()):
        if node.template or node.bound_kind != "cad" or not node.bound:
            continue
        ref = store.get_ref(kind="cad", id=node.bound)
        if ref is None:
            continue  # dangling binding is its own finding elsewhere
        try:
            spec, _handles = store.cad_load(ref.id)
            design = build_design(expand_instances(spec, design_resolver(store)))
        except (SceneError, ValueError):
            continue
        for inst_name, inst in sorted(design.instances.items()):
            prim = inst.placed.prim
            if not isinstance(prim, CylRelief):
                continue
            lay = _layout_for(prim)
            check = check_leaf(prim, inst_name, lay, prim.alpha)
            if check.ok:
                continue
            clears = (
                f"clears at k={check.min_k}"
                if check.min_k is not None
                else "no k in {2,3} clears it"
            )
            out.append(
                ValidationIssue(
                    rule=RULE,
                    subject=f"{name}.{inst_name}",
                    severity="error",
                    detail=(
                        f"relief map undercuts its slide: layout "
                        f"k={check.layout.k} phi={check.layout.phi:.4g} rad, "
                        f"alpha={check.alpha:.4g} rad — "
                        f"{check.undercut_fraction * 100:.1f}% of the "
                        f"checked surface "
                        f"(~{check.undercut_area * 1e6:.3g} mm²) "
                        f"leans out of the release cone; {clears}. "
                        f"get(kind='cad', id={node.bound!r}, view='mold') "
                        "shows the undercut image; "
                        "args={'relief_fit': 'clip'} lowers only the "
                        "overhung relief"
                    ),
                )
            )
    return out


def _layout_for(prim: CylRelief) -> SlideLayout | None:
    """The leaf's recorded pull, or ``None`` → :func:`check_leaf` falls
    back to the map's own intent then to k=3."""
    if prim.k is not None:
        return SlideLayout(k=int(prim.k), phi=float(prim.phi or 0.0))
    return None


# --------------------------------------------------------------------------
# ``mold`` op (spec slice 3) — cut the mold halves from a molded part.
#
# The geometry is the library call :func:`precis.cad.mold.cut_mold`
# (gr513564, the W5 code-first ruling — a workspace script calls it
# directly); this op is the thin wrapper that names the piece blocks,
# binds each to a fresh cad design and stamps the modes.
#
# Handler-level and store-write-deferred like ``realize``
# (:mod:`precis_se.realize`): :func:`prepare_mold` is pure over the tree
# (loads the part, refuses an undercut layout, mints the piece slugs and
# applies the block/binding/mode ops in memory); :func:`finish_mold` holds
# the ``cad_save`` of each mold piece, run only after the whole ops list
# validates, so a later op's failure never strands a minted cavity design.
# --------------------------------------------------------------------------


@dataclass
class PendingMold:
    """One ``mold`` op's deferred cad writes — the piece designs to save,
    each keyed by the block that was bound to it in :func:`prepare_mold`."""

    saves: list[tuple[str, str, SceneSpec, str]]  # (block_name, slug, spec, card)


def _mold_part_mode(node: SeBlock) -> str:
    """The ``molding`` mode to stamp on the molded part, carrying the
    part's existing material half when it declared one (so molding a
    block already typed ``.../resin`` keeps the material)."""
    if node.mode:
        try:
            _family, material = parse_mode(node.mode)
        except ModeError:
            material = None
        if material:
            return f"{MOLD_PART_MODE}/{material}"
    return MOLD_PART_MODE


def prepare_mold(
    store: Store, tree: SeTree, op: dict[str, Any], design_slug: str
) -> tuple[str, PendingMold]:
    """``{"op": "mold", "block": <part>, "layout": "halves"|"axial"|2|3,
    "alpha": <rad>, "shrink": <frac>, "margin": <m>, "phi": <rad>}`` — the
    pure half. Cuts one mold block per piece,
    ``Inter(sector_i, Diff(box, scale(part, 1+shrink)))``, named
    ``<part>.mold.<i>`` and bound to a fresh cad design; refuses a radial
    layout slice 2 finds undercut for. ``design_slug`` names the piece
    designs (threaded like ``realize``'s)."""
    name = op.get("block")
    if not name or not str(name).strip():
        raise OpError("mold needs 'block' (the part to cut a mold for)")
    key = tree.resolve_key(name)
    if key is None:
        raise OpError(f"mold: no such block {name!r}")
    node = tree.blocks[key]
    if node.bound_kind != "cad" or not node.bound:
        raise OpError(
            f"mold: block {key!r} is not bound to a cad design "
            f"(bound_kind={node.bound_kind!r}) — a mold is cut from the part's "
            "geometry, so realize/bind it to a cad design first"
        )
    ref = store.get_ref(kind="cad", id=node.bound)
    if ref is None:
        raise OpError(
            f"mold: block {key!r} binds cad design {node.bound!r}, which does "
            "not exist — fix the binding first"
        )
    try:
        spec, _handles = store.cad_load(ref.id)
        part_nodes = expand_instances(spec, design_resolver(store)).nodes
        design = build_design(spec, resolve=design_resolver(store))
    except (SceneError, ValueError, DslError) as exc:
        raise OpError(
            f"mold: cannot resolve part design {node.bound!r}: {exc}"
        ) from exc

    try:
        cut = cut_mold(
            part_nodes,
            design,
            layout=op.get("layout", "halves"),
            phi=_req_float(op, "phi", 0.0),
            alpha=_opt_float(op, "alpha"),
            shrink=_req_float(op, "shrink", 0.0),
            margin=_opt_float(op, "margin"),
        )
    except MoldUndercut as exc:
        raise _undercut_op_error(exc, key) from exc
    except ValueError as exc:
        raise OpError(f"mold: {exc}") from exc
    layout, shrink, margin = cut.layout, cut.shrink, cut.margin

    saves: list[tuple[str, str, SceneSpec, str]] = []
    block_ops: list[dict[str, Any]] = []
    for j, piece in enumerate(cut.pieces):
        block_name = f"{key}.mold.{j}"
        if block_name in tree.blocks:
            raise OpError(
                f"mold: block {block_name!r} already exists — remove the earlier "
                "mold before re-cutting"
            )
        base_slug = f"{design_slug}-{key}-mold-{j}"
        slug, _note = _unique_cad_slug(store, base_slug)
        title = f"{block_name} ({design_slug} mold)"
        card = (
            f"{title}: {layout.kind} piece {j + 1}/{layout.n_pieces}, "
            f"cavity of {key} (shrink {shrink * 100:.3g}%)"
        )
        saves.append((block_name, slug, piece, card))
        block_ops.append({"op": "add_block", "name": block_name, "parent": key})
        block_ops.append(
            {"op": "set_binding", "block": block_name, "kind": "cad", "design": slug}
        )
        block_ops.append({"op": "set_mode", "block": block_name, "mode": PIECE_MODE})
    block_ops.append({"op": "set_mode", "block": key, "mode": _mold_part_mode(node)})
    apply_ops(tree, block_ops)

    pieces = ", ".join(s[0] for s in saves)
    echo = (
        f"mold({key!r}): {layout.kind} layout, {layout.n_pieces} piece(s) "
        f"[{pieces}], shrink {shrink * 100:.3g}%, margin {margin * 1e3:.3g} mm; "
        f"part mode → {_mold_part_mode(node)!r}"
    )
    return echo, PendingMold(saves=saves)


def finish_mold(store: Store, tree: SeTree, pending: PendingMold) -> None:
    """Save each mold piece's cad design — only for a piece block a later
    op has not since removed or rebound (the ``finish_realize`` rule)."""
    for block_name, slug, spec, card in pending.saves:
        node = tree.blocks.get(block_name)
        if node is None or node.bound != slug:
            continue
        store.cad_save(
            slug=slug, title=card.split(":", 1)[0], spec=spec, card_text=card
        )


def _undercut_op_error(exc: MoldUndercut, key: str) -> OpError:
    """The op-level form of :class:`MoldUndercut`: same text the op has
    always raised (the part's block name in it) plus the ``next=`` the
    dispatch reads."""
    layout = exc.layout
    err = OpError(
        f"mold: part {key!r} undercuts the {layout.kind} k={layout.k} "
        f"phi={layout.phi:.4g} layout — "
        + "; ".join(exc.reasons)
        + ". view='mold' on the part shows the undercut image; clip the "
        "relief (args={'relief_fit':'clip'}) or choose a layout that clears it"
    )
    # Picked up by the dispatch's ``next=getattr(exc, 'next', None)``;
    # setattr because the shared OpError type declares no ``next``.
    setattr(err, "next", "run view='mold' on the part, then re-cut the mold")
    return err


def _opt_float(op: dict[str, Any], key: str) -> float | None:
    """The op's ``key`` as a float, or ``None`` when absent."""
    val = op.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError) as exc:
        raise OpError(f"mold: {key!r} must be a number, got {val!r}") from exc


def _req_float(op: dict[str, Any], key: str, default: float) -> float:
    """The op's ``key`` as a float, falling back to ``default`` when
    absent."""
    val = _opt_float(op, key)
    return default if val is None else val


def _refuse_undercut(
    design: Any, layout: MoldLayout, alpha: float | None, key: str
) -> None:
    """Refuse a radial layout slice 2 finds undercut — the standalone
    form of the check :func:`precis.cad.mold.cut_mold` also runs: one
    :func:`precis.cad.mold.mold_undercut_reasons` verdict, phrased here
    as the op's ``OpError`` + ``next=``. An axial sleeve is not gated
    (its constant-in-z undercut model is a named follow-up)."""
    reasons = mold_undercut_reasons(design, layout, alpha)
    if not reasons:
        return
    raise _undercut_op_error(MoldUndercut(layout, reasons), key)
