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

from typing import Any

from precis.cad.mold import SlideLayout, check_leaf
from precis.cad.primitives import CylRelief
from precis.cad.scene import SceneError, build_design, expand_instances
from precis.cad_resolve import design_resolver
from precis_se.ops import SeTree
from precis_se.validate import ValidationIssue

__all__ = ["RULE", "findings"]

RULE = "mold.undercut"


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
