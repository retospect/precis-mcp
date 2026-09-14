"""Manufacturing modes — how a block gets made (se-kind.md L5).

A mode key is ``family`` or ``family/material``: ``purchase``,
``fdm/asa``, ``laser/acrylic``, ``stock-cut/pipe``. This module owns the
**families** — which fabrication engines exist and what each one demands
of a block before it can be realized — and deliberately owns *no
numbers*: layer heights, kerf widths and overhang limits are versioned
capability data (``se_capabilities.json``, se-kind.md "Manufacturing
modes"), not Python constants. The line: an engine is architecture, a
process figure is data.

``purchase`` (docs/backlog/se-off-the-shelf-fabrication.md rung 1 — the
mode for a block you don't make at all) and ``atomic`` (the nm→se merge,
docs/backlog/nm-se-merge.md — realization *is* chemistry, the
:mod:`precis_se.atomic` domain layer) have implementers. The other
families are declared here so a design can be *honest about its intent*
before the implementer exists: assigning ``fdm/asa`` today records the
plan and reads back as planned-not-checked, which is the
suggestive-by-contract posture applied to L5. An unknown family is
rejected at write time (the swallowed-facet rule); a known but
unimplemented one is accepted and reported.

A family also declares **which realizations satisfy it**
(:attr:`ModeFamily.realization_kinds`) — the mode↔binding coupling
nm-se-merge.md builds. A mode is a claim about how the block gets made;
its `set_binding` is the receipt. The two disagreeing (``atomic`` mode on
a block that binds a bought `component`; a bound atomistic design on a
block whose mode says it's machined) is a contradiction between two
explicit declarations, so it surfaces as a DRC finding
(:func:`precis_se.drc.drc`, rules ``mode_without_item`` and
``mode_binding_mismatch``) — never a write-time rejection, which is the
house posture for anything a design can be mid-thought about.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModeFamily:
    """One fabrication engine. ``demands_item`` marks the families whose
    realization *is* a bought thing — the block must carry a `component`/
    `part` binding or a BOM line naming what to buy, else the mode says
    nothing. ``implemented`` is the honesty flag: False = the key is
    recordable intent, with no implementer to check it yet.

    ``realization_kinds`` is the mode↔binding coupling: the
    ``set_binding`` kinds (:data:`precis_se.ops._BINDING_KINDS`) whose
    realization satisfies this mode. Empty = the family makes no claim
    about ``bound_kind`` (a printed or milled block's solid is cad
    geometry or nothing yet — either reads fine), so the coupling check
    stays silent. Non-empty = a *different* bound kind contradicts the
    mode and reads as a DRC finding."""

    key: str
    summary: str
    implemented: bool = False
    demands_item: bool = False
    realization_kinds: tuple[str, ...] = ()


#: The fabrication engines, keyed by family. Rung order (the doc's ship
#: order), not alphabetical — the list reads as the roadmap it is.
MODE_FAMILIES: dict[str, ModeFamily] = {
    "purchase": ModeFamily(
        key="purchase",
        summary="not made — bought whole, as a component/part",
        implemented=True,
        demands_item=True,
        realization_kinds=("component", "part"),
    ),
    "fdm": ModeFamily(key="fdm", summary="fused deposition (se-kind.md slice 5)"),
    "sla": ModeFamily(key="sla", summary="resin (se-kind.md slice 6)"),
    "cnc-2.5ax": ModeFamily(
        key="cnc-2.5ax", summary="2.5-axis milling — top-reachable pockets"
    ),
    "laser": ModeFamily(
        key="laser", summary="laser-cut sheet — one profile at a stock thickness"
    ),
    "stock-cut": ModeFamily(
        key="stock-cut",
        summary="a length of stock section — cut, mitered, drilled, coped",
        demands_item=True,
        realization_kinds=("component", "part"),
    ),
    # The merged nm kind (nm-se-merge.md): an atomic-mode block's
    # realization is chemistry, so its binding is an atomistic design, and
    # the domain layer that checks it is :mod:`precis_se.atomic`. ``nm``
    # was in this tuple while that kind existed; retiring the kind left
    # ``structure`` alone as the atomic realization.
    "atomic": ModeFamily(
        key="atomic",
        summary="atomic assembler — realization is a bound atomistic design",
        implemented=True,
        realization_kinds=("structure",),
    ),
}

#: Binding kinds that can ONLY be an atomic-mode realization — the other
#: direction of the coupling. A block bound to one of these is making a
#: chemistry claim, so a non-atomic mode on it is a contradiction (and no
#: mode at all is an omission). ``cad``/``component``/``part`` are
#: deliberately absent: a cad solid or a bought part is compatible with
#: most families, and `purchase`'s own demand is checked by
#: ``demands_item``.
ATOMIC_ONLY_BINDING_KINDS: frozenset[str] = frozenset({"structure"})


class ModeError(ValueError):
    """An unknown mode family or a malformed mode key."""


def parse_mode(raw: str) -> tuple[str, str | None]:
    """``'laser/acrylic'`` → ``('laser', 'acrylic')``; ``'purchase'`` →
    ``('purchase', None)``. Raises :class:`ModeError` on an unknown family
    or an empty half, listing the legal families."""
    text = str(raw).strip()
    if not text:
        raise ModeError("mode must be a non-empty 'family' or 'family/material' key")
    family, sep, material = text.partition("/")
    family = family.strip()
    material = material.strip()
    if family not in MODE_FAMILIES:
        known = " | ".join(MODE_FAMILIES)
        raise ModeError(f"unknown mode family {family!r}; known families: {known}")
    if sep and not material:
        raise ModeError(
            f"mode {text!r} has an empty material — write '{family}' alone or "
            f"'{family}/<material>'"
        )
    return family, (material or None)


def family_of(mode: str | None) -> ModeFamily | None:
    """The :class:`ModeFamily` for a stored mode string, or ``None`` when
    the mode is unset or (hand-corrupted storage) unparseable. Never
    raises — read paths report, they don't crash."""
    if not mode:
        return None
    try:
        family, _ = parse_mode(mode)
    except ModeError:
        return None
    return MODE_FAMILIES[family]
