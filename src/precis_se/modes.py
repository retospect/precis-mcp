"""Manufacturing modes — how a block gets made (se-kind.md L5).

A mode key is ``family`` or ``family/material``: ``purchase``,
``fdm/asa``, ``laser/acrylic``, ``stock-cut/pipe``. This module owns the
**families** — which fabrication engines exist and what each one demands
of a block before it can be realized — and deliberately owns *no
numbers*: layer heights, kerf widths and overhang limits are versioned
capability data (``se_capabilities.json``, se-kind.md "Manufacturing
modes"), not Python constants. The line: an engine is architecture, a
process figure is data.

Only ``purchase`` has an implementer today (docs/backlog/
se-off-the-shelf-fabrication.md rung 1) — the mode for a block you don't
make at all. The other families are declared here so a design can be
*honest about its intent* before the implementer exists: assigning
``fdm/asa`` today records the plan and reads back as planned-not-checked,
which is the suggestive-by-contract posture applied to L5. An unknown
family is rejected at write time (the swallowed-facet rule); a known but
unimplemented one is accepted and reported.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModeFamily:
    """One fabrication engine. ``demands_item`` marks the families whose
    realization *is* a bought thing — the block must carry a `component`/
    `part` binding or a BOM line naming what to buy, else the mode says
    nothing. ``implemented`` is the honesty flag: False = the key is
    recordable intent, with no implementer to check it yet."""

    key: str
    summary: str
    implemented: bool = False
    demands_item: bool = False


#: The fabrication engines, keyed by family. Rung order (the doc's ship
#: order), not alphabetical — the list reads as the roadmap it is.
MODE_FAMILIES: dict[str, ModeFamily] = {
    "purchase": ModeFamily(
        key="purchase",
        summary="not made — bought whole, as a component/part",
        implemented=True,
        demands_item=True,
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
    ),
    "atomic": ModeFamily(
        key="atomic", summary="atomic assembler — realization is a bound nm design"
    ),
}


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
