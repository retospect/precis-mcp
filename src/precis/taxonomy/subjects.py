"""Subject axes, dispatched: one label in, every axis's parents out.

`taxonomy-bootstrap.md` §"Generated subject axes": the single entry point
(:func:`memberships`) that fans a subject label out to composition
(`composition.py`), periodic (`periodic.py`, once per element the
composition parser found), termination (`termination.py`) and the
campaign's curated ``material-class`` nodes (`config.CampaignConfig`), then
deduplicates and sorts.

Compound labels split here. ``"Pd(111)"`` is a composition label (``Pd``)
fused with a termination label (``(111)``); `composition.parse_composition`
alone fails on the parenthesis (it is not a formula), and
`termination.parse_facet` alone fails on the leading ``Pd`` (it is not a
bare facet). This module finds the facet substring, parses the remainder as
a composition, and emits edges for *both* axes off one shared child key
(`subject_key`) — the AC6 case the whole ``axis`` field on `types.AxisEdge`
exists for: ``Pd(111)`` and ``Cu(111)`` share a termination parent
(``fcc-111``) but never share a composition parent, because they are two
different child nodes whose composition edges point at ``pd`` and ``cu``
respectively.
"""

from __future__ import annotations

import re

from precis.taxonomy.composition import composition_edges, parse_composition
from precis.taxonomy.config import CampaignConfig
from precis.taxonomy.elements import element
from precis.taxonomy.periodic import periodic_edges
from precis.taxonomy.termination import facet_span, termination_edges
from precis.taxonomy.types import AxisEdge


def subject_key(label: str) -> str:
    """Lowercase, punctuation to hyphen, collapsed — a label's node key.

    The rule a widening query resolves a label to a node with. Duplicated
    (it is one line) as a private helper in `composition.py` and
    `termination.py` so those two modules never import this one, which
    imports them — see their docstrings for why.
    """
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def memberships(label: str, config: CampaignConfig) -> tuple[AxisEdge, ...]:
    """Every axis's parent proposals for ``label``, sorted and deduplicated.

    Dispatches to composition, periodic (once per element the composition
    parser found), termination and the campaign's curated domain classes;
    returns ``()`` for a label none of the axes recognise.
    """
    child = subject_key(label)
    edges: list[AxisEdge] = []

    compound_part, facet_part = _split_facet(label)
    comp = parse_composition(compound_part)
    distinct: list[str] = []
    if comp is not None:
        distinct = list(dict.fromkeys(sym for sym, _ in comp.elements))
        # The element-identity and alloy-vs-compound class parents are one
        # rule, owned by `composition.composition_edges` — reimplementing it
        # here would let the two copies drift (reviewer finding 8).
        edges.extend(_retarget(composition_edges(compound_part), child))
        for symbol in distinct:
            edges.extend(periodic_edges(symbol))
        if comp.class_suffix is not None:
            key = comp.class_suffix.lower()
            if any(dc.id == key for dc in config.domain_classes):
                edges.append(AxisEdge(child=child, parent=key, axis=config.domain_axis))

    if facet_part is not None:
        crystal_symmetry = _primary_crystal_symmetry(distinct)
        edges.extend(
            _retarget(
                termination_edges(
                    facet_part,
                    crystal_symmetry=crystal_symmetry,
                    site_classes=config.site_classes,
                ),
                child,
            )
        )
    elif config.site_classes:
        edges.extend(
            _retarget(
                termination_edges(
                    label, crystal_symmetry=None, site_classes=config.site_classes
                ),
                child,
            )
        )

    for domain_class in config.domain_classes:
        # Matched against the descriptive ``label`` ("copper catalyst"), not
        # ``id`` ("cu") — several domain-class ids are bare element symbols
        # (`cu`, `pd`) that collide with `composition_edges`' own element
        # parents on every formula containing that element; the multi-word
        # label is what a natural-language subject mention (stage 2's
        # ``subject_label``) actually contains, and a chemical formula like
        # "PdCoP" never does, so this path is inert for formula labels by
        # design and only fires on prose mentions.
        if domain_class.label and re.search(
            rf"\b{re.escape(domain_class.label)}\b", label, re.IGNORECASE
        ):
            edges.append(
                AxisEdge(child=child, parent=domain_class.id, axis=config.domain_axis)
            )

    return _dedupe_sorted(edges)


def tag_memberships(tag: str, config: CampaignConfig) -> tuple[AxisEdge, ...]:
    """Domain-class parents from a ``prefix:a-b-c`` corpus tag.

    ``config.domain_tag_prefix``-stripped, then split on
    ``config.domain_tag_separator`` — ``catalyst:sulfide-mof`` maps to both
    ``sulfide`` and ``mof`` when both are known domain-class ids. A part
    that names no known class is silently dropped rather than minting a
    node the campaign never curated.
    """
    prefix = config.domain_tag_prefix
    if prefix:
        if not tag.startswith(prefix):
            return ()
        rest = tag[len(prefix) :]
    else:
        rest = tag
    child = subject_key(tag)
    known_ids = {dc.id for dc in config.domain_classes}
    edges = [
        AxisEdge(child=child, parent=part, axis=config.domain_axis)
        for part in rest.split(config.domain_tag_separator)
        if part and part.lower() in known_ids
        for part in (part.lower(),)
    ]
    return _dedupe_sorted(edges)


def _split_facet(label: str) -> tuple[str, str | None]:
    """Pull a facet substring out of a compound label, if there is one."""
    facet_text = facet_span(label)
    if facet_text is None:
        return label, None
    rest = label.replace(facet_text, "", 1).strip()
    return rest, facet_text


def _primary_crystal_symmetry(distinct_symbols: list[str]) -> str | None:
    for symbol in distinct_symbols:
        el = element(symbol)
        if el is not None and el.crystal_symmetry is not None:
            return el.crystal_symmetry
    return None


def _retarget(edges: tuple[AxisEdge, ...], child: str) -> tuple[AxisEdge, ...]:
    """Rewrite ``edges``' child to ``child`` — see the module docstring's
    AC6 paragraph for why: `termination.termination_edges` keys off
    whatever substring it was handed, which for a compound label is the
    bare facet text, not the compound label's own node key."""
    return tuple(AxisEdge(child=child, parent=e.parent, axis=e.axis) for e in edges)


_DedupeKey = tuple[str, str, str]


def _dedupe_sorted(edges: list[AxisEdge]) -> tuple[AxisEdge, ...]:
    seen: dict[_DedupeKey, AxisEdge] = {}
    for edge in edges:
        seen[(edge.axis, edge.parent, edge.child)] = edge
    return tuple(seen[key] for key in sorted(seen, key=lambda k: (k[0], k[1], k[2])))
