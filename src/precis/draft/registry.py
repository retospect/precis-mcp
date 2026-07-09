"""Term-registry policy — the one knob that keeps three registries on one leaf.

ADR 0052: the abbreviation **glossary**, the patent **drawings/parts**
registry, and a manufacturing **components/BOM** table are one abstraction — a
family of named ``chunk_kind='term'`` leaves, discriminated by ``meta.registry``
and separated by exactly two axes: content richness (the optional attribute bag)
and **numbering policy**. This module owns the numbering policy and the
registry↔heading↔section-style bindings; everything here is **pure** (no DB, no
web) so the callout arithmetic is unit-testable in isolation.

The numbering policy collapses "taken as they go, stable" (a BOM item number
should not move when the table is re-sorted) and "assigned nicely at the end,
spaced" (patent reference numerals, ``100, 105, 110 …``) into a single 3-field
object — the difference is *data*, not two code paths (ADR §3):

* ``assign="insert"`` — the callout is **frozen into ``meta.callout`` at
  add-time**, consecutive, and stable under reorder (``components``).
* ``assign="render"`` — the callout is a **display label derived from
  reading-order position at render**, *not stored*, so inserting a part
  mid-draft renumbers the series for free (``parts``).
* ``assign="none"`` — no callout at all (``glossary``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TypedDict, TypeVar

T = TypeVar("T")


class TermEntry(TypedDict, total=False):
    """The hover record for one registry surface (ADR 0052 §4). ``definition``
    is always present; the rest of the **attribute bag** is populated only for a
    manufacturing part (absent ⇒ the leaf renders as spare as a patent part).

    A single leaf is reachable under several surface strings (its ``short``,
    each ``surface_forms`` entry, and its ``mpn``) — they all map to the same
    ``TermEntry``.
    """

    definition: str
    registry: str
    callout: int
    mpn: str
    manufacturer: str
    url: str
    ordering: str


@dataclass(frozen=True, slots=True)
class NumberingPolicy:
    """A registry's callout numbering rule. ``start``/``step`` express the
    100-boundary/step-5 aesthetic vs consecutive-from-1; ``assign`` selects
    stored-at-insert vs derived-at-render vs unnumbered."""

    start: int
    step: int
    assign: str  # "insert" | "render" | "none"


#: The registry families (``meta.registry`` values) and their numbering policy.
#: The discriminator does double duty (ADR §2): routes a leaf to its one home
#: heading and selects which derived table it projects into.
REGISTRY_POLICY: dict[str, NumberingPolicy] = {
    "glossary": NumberingPolicy(start=0, step=0, assign="none"),
    "components": NumberingPolicy(start=1, step=1, assign="insert"),
    "parts": NumberingPolicy(start=100, step=5, assign="render"),
}

#: Fallback for an unknown / unstamped leaf — an ordinary glossary term.
DEFAULT_REGISTRY = "glossary"

#: Section-style slug → the registry its ``term`` leaves belong to. The policy
#: binds to the section style via this map (ADR §5), so the style — not each
#: heading — carries the numbering behaviour.
SECTION_STYLE_REGISTRY: dict[str, str] = {
    "patent-image-part": "parts",
    "components": "components",
    "bom": "components",
}

#: Legacy home-heading titles to **adopt** (stamp ``meta.registry`` on) rather
#: than mint a duplicate when a role-tagged heading is absent (ADR §7). Matched
#: case-insensitively against a heading's trimmed text.
LEGACY_HEADING_ALIASES: dict[str, frozenset[str]] = {
    "glossary": frozenset(
        {"glossary", "abbreviations", "glossary of terms", "abbreviations and acronyms"}
    ),
    "parts": frozenset(
        {"reference numerals", "parts", "drawings", "brief description of the drawings"}
    ),
    "components": frozenset(
        {"components", "bill of materials", "bom", "parts list", "components list"}
    ),
}

#: The heading title minted for a registry that has neither a role-tagged nor a
#: legacy-adoptable home.
DEFAULT_HEADING_TITLE: dict[str, str] = {
    "glossary": "Glossary",
    "parts": "Reference Numerals",
    "components": "Components",
}


def policy_for(registry: str | None) -> NumberingPolicy:
    """The numbering policy for a registry, defaulting to the glossary
    (unnumbered) policy for an unknown / unset value."""
    return REGISTRY_POLICY.get(
        registry or DEFAULT_REGISTRY, REGISTRY_POLICY["glossary"]
    )


def registry_for_style(style: str | None) -> str:
    """The registry a section style's ``term`` leaves belong to
    (:data:`DEFAULT_REGISTRY` for a style that owns no registry)."""
    return SECTION_STYLE_REGISTRY.get(style or "", DEFAULT_REGISTRY)


def heading_title(registry: str) -> str:
    """The default home-heading title for a registry."""
    return DEFAULT_HEADING_TITLE.get(registry, "Glossary")


def next_insert_callout(existing: Iterable[int], policy: NumberingPolicy) -> int:
    """The next consecutive callout for an ``assign="insert"`` registry:
    ``max(existing) + step``, or ``start`` when the registry is empty. Stable
    under reorder because it never re-derives already-assigned numbers — a new
    part always takes the next free index and keeps it."""
    vals = [int(v) for v in existing if v is not None]
    if not vals:
        return policy.start
    return max(vals) + policy.step


def render_callouts(ordered: Sequence[T], policy: NumberingPolicy) -> dict[T, int]:
    """Spaced numerals derived from reading-order position for an
    ``assign="render"`` registry: the i-th item (0-based) → ``start + i*step``.
    Recomputed every render, so inserting/reordering a leaf renumbers the whole
    series and the spacing stays clean and boundary-aligned (ADR §3)."""
    return {item: policy.start + i * policy.step for i, item in enumerate(ordered)}
